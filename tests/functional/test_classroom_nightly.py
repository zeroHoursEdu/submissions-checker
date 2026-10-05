"""Nightly Classroom job: grading runner (grade_one / loop / reaper) and the cron entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import text

from submissions_checker.api.routes import teacher_classroom
from submissions_checker.core.config import Settings, get_settings
from submissions_checker.db.models import Subject, SubjectsAssignment
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import SubjectStatus
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.main import app
from submissions_checker.services.google import ingest as ingest_module
from submissions_checker.services.google.crypto import encrypt_token
from submissions_checker.services.google.ingest import ADVISORY_LOCK_KEY
from submissions_checker.services.google.matching import RosterEntry
from submissions_checker.services.google.oauth import GoogleAuthError
from submissions_checker.services.llm_grading import runner
from submissions_checker.services.llm_grading.judge import (
    CriterionVerdict,
    GradingRequest,
    GradingResult,
    JudgeError,
)
from submissions_checker.workers.scheduled import classroom_nightly

pytestmark = pytest.mark.asyncio

KEY = Fernet.generate_key().decode()
KYIV = ZoneInfo("Europe/Kyiv")
NIGHT = datetime(2026, 10, 5, 3, 10, tzinfo=KYIV).astimezone(UTC)
NIGHT_START = datetime(2026, 10, 5, 0, 0, tzinfo=KYIV).astimezone(UTC)
DEADLINE = datetime(2026, 10, 5, 4, 0, tzinfo=KYIV).astimezone(UTC)
# Rows arranged by tests were last touched before tonight unless a test says otherwise.
EARLIER = NIGHT - timedelta(days=1)
TASK = "Write a report about sorting algorithms."


def _config(code: str) -> dict[str, Any]:
    return {
        "review_mode": "quiz_and_teacher_scores",
        "llm_grading": {
            "enabled": True,
            "source": "google_classroom",
            "task": f"{TASK} ({code})",
            "instructions": "Be strict.",
        },
        "grading": {
            "teacher_criteria": [
                {"key": "intro", "title": "Вступ", "max": 4, "requirements": "Has an intro."},
                {"key": "body", "max": 6, "requirements": "Explains quicksort."},
                {"key": "oral", "title": "Захист", "max": 5, "llm": False},
            ]
        },
    }


def _settings(**overrides: Any) -> Settings:
    return Settings(
        secret_key="test-secret-key-minimum-32-chars-long",
        google_client_id="cid",
        google_client_secret="csecret",
        google_token_encryption_key=KEY,
        **overrides,
    )


class FakeJudge:
    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[GradingRequest] = []
        self.closed = False

    async def grade(self, req: GradingRequest) -> GradingResult:
        self.calls.append(req)
        if self.fail:
            raise JudgeError("judge answer invalid twice: points out of range")
        return GradingResult(
            criteria={
                c.key: CriterionVerdict(c.key, c.max - 1, f"ok {c.key}", f"quote {c.key}")
                for c in req.criteria
            },
            comment="Good work",
            provider="claude_cli",
            model="opus",
            usage={"input_tokens": 1200, "output_tokens": 300},
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = data
        return f"s3://{key}"


@pytest.fixture
async def world(db, teacher):
    subject = Subject(name="S", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    a1 = SubjectsAssignment(subject_id=subject.id, title="Lab1", code="lab1", config=_config("1"))
    a2 = SubjectsAssignment(subject_id=subject.id, title="Lab2", code="lab2", config=_config("2"))
    db.add_all([a1, a2])
    await db.commit()
    link = ClassroomStudentLink(
        subject_id=subject.id, classroom_user_id="u1", classroom_name="ІП-43 A B", method="NAME"
    )
    db.add(link)
    await db.commit()
    return {"subject": subject, "a1": a1, "a2": a2, "link": link, "storage": FakeStorage()}


_seq = {"n": 0}


async def _grading(
    db,
    world,
    *,
    assignment=None,
    status: str = "PENDING",
    attempts: int = 0,
    manifest: list[dict[str, Any]] | None = None,
    updated_at: datetime = EARLIER,
) -> LLMGrading:
    _seq["n"] += 1
    n = _seq["n"]
    asg = assignment or world["a1"]
    if manifest is None:
        key = f"classroom/{n}/report.pdf"
        world["storage"].objects[key] = f"pdf bytes {n}".encode()
        manifest = [
            {
                "drive_id": f"d{n}",
                "name": "report.pdf",
                "mime": "application/pdf",
                "size": 10,
                "modified": "2026-10-01T10:00:00Z",
                "sha256": "a" * 64,
                "storage_key": key,
                "skipped": None,
            },
            {
                "drive_id": f"x{n}",
                "name": "video.mp4",
                "mime": "video/mp4",
                "size": 10**9,
                "modified": "2026-10-01T10:00:00Z",
                "sha256": None,
                "storage_key": None,
                "skipped": "too_large",
            },
        ]
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=world["link"].id,
        classroom_submission_id=f"s{n}",
        state="TURNED_IN",
        content_hash=f"{n:064d}",
        manifest=manifest,
        seen_at=datetime.now(UTC),
    )
    db.add(work)
    await db.commit()
    grading = LLMGrading(
        classroom_work_id=work.id, status=status, attempts=attempts, updated_at=updated_at
    )
    db.add(grading)
    await db.commit()
    return grading


async def _reload(db, grading_id: int) -> LLMGrading:
    db.expire_all()
    g = await db.get(LLMGrading, grading_id)
    assert g is not None
    return g


def _loop_kwargs(now: datetime = NIGHT, cap: int = 40) -> dict[str, Any]:
    return {"now_fn": lambda: now, "deadline": DEADLINE, "cap": cap, "night_start": NIGHT_START}


async def _advisory_holders(engine) -> int:
    """Granted advisory locks on our key (a bigint key is split into classid/objid)."""
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND classid = :hi AND objid = :lo AND objsubid = 1 AND granted"
                    ),
                    {"hi": ADVISORY_LOCK_KEY >> 32, "lo": ADVISORY_LOCK_KEY & 0xFFFFFFFF},
                )
            ).scalar_one()
        )


# ── build_request / grade_one ────────────────────────────────────────────────


async def test_build_request_uses_config_and_gradable_files(db, world):
    g = await _grading(db, world)
    req = await runner.build_request(db, g, world["storage"])
    assert req.task == f"{TASK} (1)"
    assert req.instructions == "Be strict."
    assert [(c.key, c.title, c.max, c.requirements) for c in req.criteria] == [
        ("intro", "Вступ", 4, "Has an intro."),
        ("body", "body", 6, "Explains quicksort."),
    ]
    assert [(f.name, f.mime) for f in req.files] == [("report.pdf", "application/pdf")]
    assert req.files[0].content.startswith(b"pdf bytes")


async def test_grade_one_done_stores_draft(db, world):
    g = await _grading(db, world)
    judge = FakeJudge()
    await runner.grade_one(db, g, judge, world["storage"])
    g = await _reload(db, g.id)
    assert g.status == "DONE"
    assert (g.provider, g.model, g.error) == ("claude_cli", "opus", None)
    assert g.graded_at is not None
    assert g.attempts == 0
    assert g.draft == {
        "criteria": {
            "intro": {"points": 3, "justification": "ok intro", "evidence": "quote intro"},
            "body": {"points": 5, "justification": "ok body", "evidence": "quote body"},
        },
        "comment": "Good work",
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }
    assert len(judge.calls) == 1


async def test_invalid_twice_marks_failed(db, world):
    g = await _grading(db, world)
    await runner.grade_one(db, g, FakeJudge(fail=True), world["storage"])
    g = await _reload(db, g.id)
    assert g.status == "FAILED"
    assert g.attempts == 1
    assert g.error == "judge answer invalid twice: points out of range"
    assert g.draft is None


async def test_judge_unreachable_is_retryable_failure(db, world):
    class Down(FakeJudge):
        async def grade(self, req: GradingRequest) -> GradingResult:
            raise JudgeError("judge unreachable: connection reset")

    g = await _grading(db, world, status="FAILED", attempts=1)
    await runner.grade_one(db, g, Down(), world["storage"])
    g = await _reload(db, g.id)
    assert (g.status, g.attempts) == ("FAILED", 2)
    assert g.error is not None and g.error.startswith("judge unreachable")


async def test_storage_failure_marks_failed(db, world):
    g = await _grading(db, world)
    world["storage"].objects.clear()
    judge = FakeJudge()
    await runner.grade_one(db, g, judge, world["storage"])
    g = await _reload(db, g.id)
    assert (g.status, g.attempts) == ("FAILED", 1)
    assert judge.calls == []


async def test_long_error_is_truncated(db, world):
    class Verbose(FakeJudge):
        async def grade(self, req: GradingRequest) -> GradingResult:
            raise JudgeError("x" * 5000)

    g = await _grading(db, world)
    await runner.grade_one(db, g, Verbose(), world["storage"])
    g = await _reload(db, g.id)
    assert g.error == "x" * 2000


async def test_no_gradable_files_fails_without_judge(db, world):
    manifest = [
        {
            "drive_id": "d",
            "name": "video.mp4",
            "mime": "video/mp4",
            "size": 1,
            "modified": "",
            "sha256": None,
            "storage_key": None,
            "skipped": "unsupported",
        }
    ]
    g = await _grading(db, world, manifest=manifest)
    judge = FakeJudge()
    await runner.grade_one(db, g, judge, world["storage"])
    g = await _reload(db, g.id)
    assert (g.status, g.error, g.attempts) == ("FAILED", "no_gradable_files", runner.MAX_ATTEMPTS)
    assert judge.calls == []


# ── grading_loop ─────────────────────────────────────────────────────────────


async def test_loop_respects_cap(db, world, functional_sessionmaker):
    ids = [(await _grading(db, world)).id for _ in range(5)]
    judge = FakeJudge()
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs(cap=2)
    )
    assert done == 2
    statuses = [(await _reload(db, i)).status for i in ids]
    assert statuses == ["DONE", "DONE", "PENDING", "PENDING", "PENDING"]


async def test_loop_stops_at_deadline(db, world, functional_sessionmaker):
    g = await _grading(db, world)
    judge = FakeJudge()
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs(now=DEADLINE)
    )
    assert done == 0
    assert judge.calls == []
    assert (await _reload(db, g.id)).status == "PENDING"


async def test_loop_stops_when_clock_passes_deadline(db, world, functional_sessionmaker):
    ids = [(await _grading(db, world)).id for _ in range(3)]
    clock = {"t": NIGHT}

    class Slow(FakeJudge):
        async def grade(self, req: GradingRequest) -> GradingResult:
            clock["t"] = DEADLINE + timedelta(minutes=1)
            return await super().grade(req)

    done = await runner.grading_loop(
        functional_sessionmaker,
        Slow(),
        world["storage"],
        now_fn=lambda: clock["t"],
        deadline=DEADLINE,
        cap=40,
        night_start=NIGHT_START,
    )
    assert done == 1
    assert [(await _reload(db, i)).status for i in ids] == ["DONE", "PENDING", "PENDING"]


async def test_loop_skips_exhausted_failed(db, world, functional_sessionmaker):
    exhausted = (await _grading(db, world, status="FAILED", attempts=3)).id
    retry = (await _grading(db, world, status="FAILED", attempts=1)).id
    judge = FakeJudge()
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs()
    )
    assert done == 1
    assert (await _reload(db, exhausted)).status == "FAILED"
    assert (await _reload(db, retry)).status == "DONE"


async def test_loop_does_not_retry_a_failure_in_the_same_run(db, world, functional_sessionmaker):
    g = await _grading(db, world)
    judge = FakeJudge(fail=True)
    await runner.grading_loop(functional_sessionmaker, judge, world["storage"], **_loop_kwargs())
    assert len(judge.calls) == 1
    assert (await _reload(db, g.id)).attempts == 1


async def test_loop_ignores_waiting_link_and_finished(db, world, functional_sessionmaker):
    waiting = (await _grading(db, world, status="WAITING_LINK")).id
    done_row = (await _grading(db, world, status="DONE")).id
    running = (await _grading(db, world, status="RUNNING")).id
    judge = FakeJudge()
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs()
    )
    assert done == 0
    assert judge.calls == []
    assert (await _reload(db, waiting)).status == "WAITING_LINK"
    assert (await _reload(db, done_row)).status == "DONE"
    assert (await _reload(db, running)).status == "RUNNING"


async def test_loop_stops_after_three_consecutive_judge_errors(db, world, functional_sessionmaker):
    ids = [(await _grading(db, world)).id for _ in range(5)]
    judge = FakeJudge(fail=True)
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs()
    )
    assert done == 3
    assert len(judge.calls) == 3
    rows = []
    for i in ids:
        r = await _reload(db, i)
        rows.append((r.status, r.attempts))
    assert rows == [
        ("FAILED", 1),
        ("FAILED", 1),
        ("FAILED", 1),
        ("PENDING", 0),
        ("PENDING", 0),
    ]


async def test_judge_error_streak_resets_on_success(db, world, functional_sessionmaker):
    for _ in range(5):
        await _grading(db, world)
    outcomes = iter([True, True, False, True, True])

    class Flaky(FakeJudge):
        async def grade(self, req: GradingRequest) -> GradingResult:
            self.fail = next(outcomes)
            return await super().grade(req)

    judge = Flaky()
    await runner.grading_loop(functional_sessionmaker, judge, world["storage"], **_loop_kwargs())
    assert len(judge.calls) == 5


async def test_loop_budget_counts_tonights_finished_jobs(db, world, functional_sessionmaker):
    # Two gradings already finished tonight (one DONE, one FAILED) use up the budget.
    done_row = await _grading(db, world, status="DONE", updated_at=NIGHT)
    done_row.graded_at = NIGHT - timedelta(minutes=30)
    await _grading(db, world, status="FAILED", attempts=1, updated_at=NIGHT)
    await db.commit()
    pending = (await _grading(db, world)).id
    judge = FakeJudge()
    done = await runner.grading_loop(
        functional_sessionmaker, judge, world["storage"], **_loop_kwargs(cap=2)
    )
    assert done == 0
    assert (await _reload(db, pending)).status == "PENDING"


async def test_loop_orders_by_assignment(db, world, functional_sessionmaker):
    await _grading(db, world, assignment=world["a2"])
    await _grading(db, world, assignment=world["a1"])
    await _grading(db, world, assignment=world["a2"])
    await _grading(db, world, assignment=world["a1"])
    judge = FakeJudge()
    await runner.grading_loop(functional_sessionmaker, judge, world["storage"], **_loop_kwargs())
    assert [c.task[-3:] for c in judge.calls] == ["(1)", "(1)", "(2)", "(2)"]


# ── reap_stale ───────────────────────────────────────────────────────────────


async def test_reap_stale_running(db, world):
    stale = (await _grading(db, world, status="RUNNING", updated_at=NIGHT - timedelta(hours=2))).id
    fresh = (
        await _grading(db, world, status="RUNNING", updated_at=NIGHT - timedelta(minutes=10))
    ).id
    assert await runner.reap_stale(db, NIGHT) == 1
    s = await _reload(db, stale)
    assert (s.status, s.error, s.attempts) == ("FAILED", "stale", 1)
    assert (await _reload(db, fresh)).status == "RUNNING"


# ── run_classroom_nightly ────────────────────────────────────────────────────


class FakeClassroom:
    """ClassroomClient stand-in: one per refresh token, records what it was asked."""

    instances: dict[str, FakeClassroom] = {}

    def __init__(self, settings: Settings, refresh_token: str, http: Any) -> None:
        self.refresh_token = refresh_token
        self.roster_calls = 0
        FakeClassroom.instances[refresh_token] = self

    async def list_students(self, course_id: str) -> list[RosterEntry]:
        self.roster_calls += 1
        if self.refresh_token == "dead":
            raise GoogleAuthError("dead", invalid_grant=True)
        return [RosterEntry("u9", "ІП-43 Komin Ivan", "komin@edu.kpi.ua")]

    async def list_submissions(self, course_id: str, coursework_id: str) -> list[Any]:
        return []

    async def list_courses(self) -> list[dict[str, str]]:
        return []

    async def list_coursework(self, course_id: str) -> list[dict[str, str]]:
        return []


@pytest.fixture
def nightly_env(monkeypatch, functional_engine, functional_sessionmaker, world):
    FakeClassroom.instances = {}
    judge = FakeJudge()
    overrides: dict[str, Any] = {}
    clock = {"now": NIGHT}
    monkeypatch.setattr(classroom_nightly, "get_settings", lambda: _settings(**overrides))
    monkeypatch.setattr(classroom_nightly, "get_engine", lambda: functional_engine)
    monkeypatch.setattr(classroom_nightly, "get_session_factory", lambda: functional_sessionmaker)
    monkeypatch.setattr(classroom_nightly, "get_storage", lambda _s: world["storage"])
    monkeypatch.setattr(classroom_nightly, "get_judge", lambda _s: judge)
    monkeypatch.setattr(classroom_nightly, "ClassroomClient", FakeClassroom)
    monkeypatch.setattr(classroom_nightly, "_now", lambda: clock["now"])
    return {**world, "judge": judge, "overrides": overrides, "clock": clock}


async def _linked_subject(db, make_user, name: str, refresh_token: str) -> Subject:
    owner = await make_user(username=f"t_{name}")
    conn = GoogleConnection(
        user_id=owner.id,
        google_email=f"{name}@edu.kpi.ua",
        refresh_token_enc=encrypt_token(_settings(), refresh_token),
    )
    subject = Subject(name=name, owner_id=owner.id)
    db.add_all([conn, subject])
    await db.commit()
    subject.classroom_course_id = f"course-{name}"
    subject.classroom_connection_id = conn.id
    await db.commit()
    return subject


async def test_nightly_ingests_then_grades_and_closes_judge(db, nightly_env, make_user):
    subject_id = (await _linked_subject(db, make_user, "B", "rtB")).id
    g = await _grading(db, nightly_env)
    await classroom_nightly.run_classroom_nightly()
    assert FakeClassroom.instances["rtB"].roster_calls == 1
    assert len(nightly_env["judge"].calls) == 1
    assert nightly_env["judge"].closed
    assert (await _reload(db, g.id)).status == "DONE"
    db.expire_all()
    s = await db.get(Subject, subject_id)
    assert s is not None and s.classroom_synced_at is not None


async def test_nightly_reaps_stale_before_grading(db, nightly_env):
    gid = (
        await _grading(db, nightly_env, status="RUNNING", updated_at=NIGHT - timedelta(hours=3))
    ).id
    await classroom_nightly.run_classroom_nightly()
    # Reaped to FAILED(attempts 1) tonight, so it waits for the next night.
    g = await _reload(db, gid)
    assert (g.status, g.attempts, g.error) == ("FAILED", 1, "stale")
    assert nightly_env["judge"].calls == []


async def test_nightly_skips_when_lock_held(db, nightly_env, make_user, functional_engine):
    await _linked_subject(db, make_user, "B", "rtB")
    g = await _grading(db, nightly_env)
    async with functional_engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        await other.commit()
        try:
            await classroom_nightly.run_classroom_nightly()
        finally:
            await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    assert FakeClassroom.instances == {}
    assert nightly_env["judge"].calls == []
    assert (await _reload(db, g.id)).status == "PENDING"


async def test_nightly_releases_lock(db, nightly_env, functional_engine):
    await classroom_nightly.run_classroom_nightly()
    assert await _advisory_holders(functional_engine) == 0


async def test_lock_connection_invalidated_when_unlock_fails(functional_engine, monkeypatch):
    async def broken(conn):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(ingest_module, "unlock_classroom", broken)
    async with ingest_module.classroom_lock(functional_engine) as locked:
        assert locked
        assert await _advisory_holders(functional_engine) == 1
    # The pool must not keep a connection that still holds the lock.
    assert await _advisory_holders(functional_engine) == 0


async def test_dst_fall_back_double_firing_shares_one_budget(db, nightly_env):
    # 2026-10-25: 03:00 local happens twice, the cron fires at 00:00Z and at 01:00Z.
    nightly_env["overrides"]["llm_grading_nightly_cap"] = 3
    for _ in range(5):
        await _grading(db, nightly_env)
    nightly_env["clock"]["now"] = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    await classroom_nightly.run_classroom_nightly()
    nightly_env["clock"]["now"] = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    await classroom_nightly.run_classroom_nightly()
    assert len(nightly_env["judge"].calls) == 3


async def test_dst_fall_back_second_run_does_not_retry_tonights_failure(db, nightly_env):
    nightly_env["overrides"]["llm_grading_nightly_cap"] = 4
    first = (await _grading(db, nightly_env)).id
    await _grading(db, nightly_env)
    outcomes = iter([True, False, False])
    judge = nightly_env["judge"]
    original = FakeJudge.grade

    async def grade(req: GradingRequest) -> GradingResult:
        judge.fail = next(outcomes)
        return await original(judge, req)

    judge.grade = grade  # type: ignore[method-assign]
    nightly_env["clock"]["now"] = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    await classroom_nightly.run_classroom_nightly()
    late = (await _grading(db, nightly_env)).id
    nightly_env["clock"]["now"] = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    await classroom_nightly.run_classroom_nightly()
    assert len(judge.calls) == 3
    g = await _reload(db, first)
    assert (g.status, g.attempts) == ("FAILED", 1)
    assert (await _reload(db, late)).status == "DONE"


async def test_dst_spring_forward_run_still_grades(db, nightly_env):
    # 2027-03-28: the cron fires at 01:00Z, which is already 04:00 local.
    gid = (await _grading(db, nightly_env)).id
    nightly_env["clock"]["now"] = datetime(2027, 3, 28, 1, 0, tzinfo=UTC)
    await classroom_nightly.run_classroom_nightly()
    assert len(nightly_env["judge"].calls) == 1
    assert (await _reload(db, gid)).status == "DONE"


async def test_nightly_skips_archived_subject(db, nightly_env, make_user):
    subject = await _linked_subject(db, make_user, "A", "rtA")
    subject.status = SubjectStatus.DELETED
    await db.commit()
    await classroom_nightly.run_classroom_nightly()
    assert FakeClassroom.instances == {}


async def test_nightly_continues_after_subject_auth_error(db, nightly_env, make_user):
    a = (await _linked_subject(db, make_user, "A", "dead")).id
    b = (await _linked_subject(db, make_user, "B", "rtB")).id
    g = (await _grading(db, nightly_env)).id
    await classroom_nightly.run_classroom_nightly()
    assert FakeClassroom.instances["dead"].roster_calls == 1
    assert FakeClassroom.instances["rtB"].roster_calls == 1
    db.expire_all()
    sa = await db.get(Subject, a)
    sb = await db.get(Subject, b)
    assert sa is not None and sa.classroom_sync_error == "reconnect"
    assert sb is not None and sb.classroom_synced_at is not None
    assert (await _reload(db, g)).status == "DONE"


async def test_nightly_skips_inactive_connection_and_unlinked(db, nightly_env, make_user):
    dead = await _linked_subject(db, make_user, "A", "rtA")
    conn = await db.get(GoogleConnection, dead.classroom_connection_id)
    assert conn is not None
    conn.status = "ERROR"
    unlinked = await _linked_subject(db, make_user, "C", "rtC")
    unlinked.classroom_course_id = None
    await db.commit()
    await classroom_nightly.run_classroom_nightly()
    assert FakeClassroom.instances == {}


async def test_nightly_without_storage_skips_ingest_and_grading(
    db, nightly_env, make_user, monkeypatch
):
    monkeypatch.setattr(classroom_nightly, "get_storage", lambda _s: None)
    await _linked_subject(db, make_user, "B", "rtB")
    g = await _grading(db, nightly_env)
    await classroom_nightly.run_classroom_nightly()
    assert FakeClassroom.instances == {}
    assert nightly_env["judge"].calls == []
    assert (await _reload(db, g.id)).status == "PENDING"


# ── Manual sync shares the lock ──────────────────────────────────────────────


async def test_sync_route_busy_while_nightly_holds_lock(
    teacher_client: AsyncClient, db, teacher, world, monkeypatch, functional_engine
):
    conn = GoogleConnection(
        user_id=teacher.id,
        google_email="t@edu.kpi.ua",
        refresh_token_enc=encrypt_token(_settings(), "rt"),
    )
    db.add(conn)
    await db.commit()
    subject = world["subject"]
    subject.classroom_course_id = "c1"
    subject.classroom_connection_id = conn.id
    await db.commit()
    fake = FakeClassroom(_settings(), "rt", None)
    app.dependency_overrides[get_settings] = lambda: _settings()
    monkeypatch.setattr(teacher_classroom, "_client", lambda *a: fake)
    monkeypatch.setattr(teacher_classroom, "get_storage", lambda _s: world["storage"])
    try:
        async with functional_engine.connect() as other:
            await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
            await other.commit()
            try:
                resp = await teacher_client.post(f"/teacher/subjects/{subject.id}/classroom/sync")
            finally:
                await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("classroom_error=busy")
        assert fake.roster_calls == 0
        page = await teacher_client.get(resp.headers["location"])
        assert "Синхронізація вже виконується" in page.text

        # Lock free again: the same button syncs and releases the lock afterwards.
        resp = await teacher_client.post(f"/teacher/subjects/{subject.id}/classroom/sync")
        assert "classroom=synced" in resp.headers["location"]
        assert fake.roster_calls == 1
        assert await _advisory_holders(functional_engine) == 0
    finally:
        app.dependency_overrides.pop(get_settings, None)
