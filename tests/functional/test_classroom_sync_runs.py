"""Manual Classroom sync runs in the background and reports progress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes import teacher_classroom
from submissions_checker.core.config import get_settings
from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.classroom import ClassroomSyncRun
from submissions_checker.db.models.enums import UserRole
from submissions_checker.main import app
from submissions_checker.services.google import sync_runs
from submissions_checker.services.google.ingest import SyncProgress, ingest_subject
from submissions_checker.services.google.oauth import GoogleAuthError
from tests.functional.test_classroom_ingest import (  # noqa: F401 — fixture
    _settings,
    _three_submissions,
    _works,
    setup,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch, setup):  # noqa: F811
    app.dependency_overrides[get_settings] = _settings
    monkeypatch.setattr(teacher_classroom, "_client", lambda *a: setup["client"])
    monkeypatch.setattr(teacher_classroom, "get_storage", lambda _s: setup["storage"])
    yield setup
    app.dependency_overrides.pop(get_settings, None)


async def _runs(db) -> list[ClassroomSyncRun]:
    db.expire_all()
    return list(
        (await db.execute(select(ClassroomSyncRun).order_by(ClassroomSyncRun.id))).scalars()
    )


async def test_ingest_reports_progress(db, setup):  # noqa: F811
    _three_submissions(setup["client"])
    seen: list[SyncProgress] = []

    async def on_progress(p: SyncProgress) -> None:
        seen.append(p)

    await ingest_subject(db, setup["subject"], setup["client"], setup["storage"], on_progress)

    assert seen[0].assignments_total == 1 and seen[0].assignments_done == 0
    last = seen[-1]
    assert (last.assignments_done, last.submissions_done, last.submissions_total) == (1, 3, 3)
    assert last.new_versions == 3


async def test_sync_returns_at_once_and_finishes_in_background(
    teacher_client: AsyncClient, db, env
):
    _three_submissions(env["client"])
    sid = env["subject_id"]

    resp = await teacher_client.post(f"/teacher/subjects/{sid}/classroom/sync")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{sid}?classroom=sync_started"
    await sync_runs.wait_for_background()
    [run] = await _runs(db)
    assert run.status == "DONE" and run.finished_at is not None
    assert (run.new_versions, run.submissions_done, run.assignments_done) == (3, 3, 1)
    assert len(await _works(db)) == 3
    audit = (
        await db.execute(select(AuditLog).where(AuditLog.action == "classroom_synced"))
    ).scalar_one()
    assert audit.target_id == sid and audit.detail["new_versions"] == 3


async def test_status_lists_own_runs_with_progress(teacher_client: AsyncClient, db, env, teacher):
    sid = env["subject_id"]
    now = datetime.now(UTC)
    db.add(
        ClassroomSyncRun(
            subject_id=sid,
            started_by=teacher.id,
            status="RUNNING",
            assignments_total=2,
            assignments_done=1,
            submissions_total=4,
            submissions_done=2,
            heartbeat_at=now,
        )
    )
    await db.commit()

    body = (await teacher_client.get("/teacher/classroom/sync/active")).json()

    [run] = body["runs"]
    assert run["status"] == "running" and run["subject_id"] == sid
    assert run["subject_name"] == "S"
    assert run["percent"] == 75  # (1 + 2/4) / 2
    assert run["url"] == f"/teacher/subjects/{sid}?tab=ops"


async def test_silent_running_run_reads_as_interrupted(
    teacher_client: AsyncClient, db, env, teacher
):
    db.add(
        ClassroomSyncRun(
            subject_id=env["subject_id"],
            started_by=teacher.id,
            status="RUNNING",
            heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    await db.commit()
    [run] = (await teacher_client.get("/teacher/classroom/sync/active")).json()["runs"]
    assert run["status"] == "interrupted"


async def test_old_finished_runs_are_not_listed(teacher_client: AsyncClient, db, env, teacher):
    db.add(
        ClassroomSyncRun(
            subject_id=env["subject_id"],
            started_by=teacher.id,
            status="DONE",
            heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db.commit()
    assert (await teacher_client.get("/teacher/classroom/sync/active")).json()["runs"] == []


async def test_other_teachers_runs_are_not_listed(client: AsyncClient, db, env, login, make_user):
    db.add(
        ClassroomSyncRun(
            subject_id=env["subject_id"], status="RUNNING", heartbeat_at=datetime.now(UTC)
        )
    )
    await db.commit()
    other = await make_user(role=UserRole.TEACHER, username="other")
    login(client, other)
    assert (await client.get("/teacher/classroom/sync/active")).json()["runs"] == []


async def test_second_sync_while_running_is_refused(teacher_client: AsyncClient, db, env):
    sid = env["subject_id"]
    db.add(ClassroomSyncRun(subject_id=sid, status="RUNNING", heartbeat_at=datetime.now(UTC)))
    await db.commit()
    resp = await teacher_client.post(f"/teacher/subjects/{sid}/classroom/sync")
    assert resp.headers["location"].endswith("classroom_error=busy")
    assert len(await _runs(db)) == 1


async def test_interrupted_run_does_not_block_a_new_sync(teacher_client: AsyncClient, db, env):
    sid = env["subject_id"]
    db.add(
        ClassroomSyncRun(
            subject_id=sid,
            status="RUNNING",
            heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    await db.commit()
    resp = await teacher_client.post(f"/teacher/subjects/{sid}/classroom/sync")
    assert resp.headers["location"].endswith("classroom=sync_started")
    await sync_runs.wait_for_background()
    old, new = await _runs(db)
    assert (old.status, old.error) == ("FAILED", "interrupted")
    assert new.status == "DONE"


async def test_background_failure_is_recorded(teacher_client: AsyncClient, db, env):
    env["client"].roster_error = GoogleAuthError("dead", invalid_grant=True)
    await teacher_client.post(f"/teacher/subjects/{env['subject_id']}/classroom/sync")
    await sync_runs.wait_for_background()
    [run] = await _runs(db)
    assert (run.status, run.error) == ("FAILED", "reconnect")


async def test_held_lock_fails_the_run_as_busy(teacher_client: AsyncClient, db, env, monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _taken(_engine):
        yield False

    monkeypatch.setattr(sync_runs, "classroom_lock", _taken)
    await teacher_client.post(f"/teacher/subjects/{env['subject_id']}/classroom/sync")
    await sync_runs.wait_for_background()
    [run] = await _runs(db)
    assert (run.status, run.error) == ("FAILED", "busy")


async def test_teacher_pages_carry_the_progress_widget(teacher_client: AsyncClient, db, env):
    page = await teacher_client.get(f"/teacher/subjects/{env['subject_id']}")
    assert 'id="sync-progress"' in page.text
    assert "/teacher/classroom/sync/active" in page.text


async def test_student_pages_do_not(student_client: AsyncClient):
    page = await student_client.get("/portal")
    assert 'id="sync-progress"' not in page.text
