"""Manual "sync now" runs: started from a request, carried out in a background task.

The request only records a RUNNING row and returns, so the page never hangs on a slow
Classroom and a deploy cannot leave the teacher staring at a spinner. Progress goes to the
row through its own session (committed independently of the ingest transaction), so any
replica can answer the progress poll. A RUNNING row whose heartbeat stopped was killed with
its process and reads as interrupted; it never blocks the next sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.classroom import ClassroomSyncRun
from submissions_checker.db.models.enums import ClassroomSyncStatus
from submissions_checker.db.models.subject import Subject
from submissions_checker.services.audit import audit
from submissions_checker.services.google.client import GoogleApiError
from submissions_checker.services.google.ingest import (
    ClassroomApi,
    ObjectStore,
    SyncProgress,
    classroom_lock,
    ingest_subject,
)
from submissions_checker.services.google.oauth import GoogleAuthError

logger = get_logger(__name__)

# Longer than any single step (one file download is capped at 40 MB).
STALE_AFTER = timedelta(minutes=5)
# A finished run stays in the panel this long, so the teacher sees how it ended.
SHOW_FINISHED_FOR = timedelta(minutes=10)
GOOGLE_TIMEOUT = 20.0

_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class Actor:
    user_id: int
    username: str


def _now() -> datetime:
    return datetime.now(UTC)


def is_stale(run: ClassroomSyncRun, now: datetime) -> bool:
    return run.status == ClassroomSyncStatus.RUNNING and now - run.heartbeat_at > STALE_AFTER


async def start(db: AsyncSession, subject_id: int, user_id: int) -> ClassroomSyncRun | None:
    """A new RUNNING row, or None while another live run exists for the subject.

    Commits. Rows left RUNNING by a killed process are closed as interrupted first.
    """
    now = _now()
    running = (
        await db.execute(
            select(ClassroomSyncRun)
            .where(
                ClassroomSyncRun.subject_id == subject_id,
                ClassroomSyncRun.status == ClassroomSyncStatus.RUNNING.value,
            )
            .with_for_update()
        )
    ).scalars()
    for old in running:
        if not is_stale(old, now):
            await db.rollback()
            return None
        old.status = ClassroomSyncStatus.FAILED.value
        old.error = "interrupted"
        old.finished_at = now
    run = ClassroomSyncRun(
        subject_id=subject_id,
        started_by=user_id,
        status=ClassroomSyncStatus.RUNNING.value,
        heartbeat_at=now,
    )
    db.add(run)
    await db.commit()
    return run


def spawn(coro: Coroutine[Any, Any, None]) -> None:
    """Run in the background, keeping a reference so the task is not collected midway."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_for_background() -> None:
    """Await every running sync task (tests; nothing in the app waits on them)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def _update(sessions: async_sessionmaker[AsyncSession], run_id: int, **fields: Any) -> None:
    async with sessions() as db:
        run = await db.get(ClassroomSyncRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        run.heartbeat_at = _now()
        await db.commit()


async def run(
    engine: AsyncEngine,
    run_id: int,
    subject_id: int,
    make_client: Callable[[httpx.AsyncClient], ClassroomApi],
    storage: ObjectStore,
    actor: Actor,
) -> None:
    """The background half of a manual sync. Never raises: the outcome lands on the row."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def on_progress(p: SyncProgress) -> None:
        try:
            await _update(sessions, run_id, **asdict(p))
        except Exception:  # noqa: BLE001 — a lost progress tick must not abort the sync
            logger.warning("classroom_sync_progress_failed", run_id=run_id, exc_info=True)

    error: str | None = None
    try:
        async with classroom_lock(engine) as locked:
            if not locked:  # the nightly job (or a sync on another replica) is ingesting
                error = "busy"
            else:
                async with sessions() as db, httpx.AsyncClient(timeout=GOOGLE_TIMEOUT) as http:
                    subject = await db.get(Subject, subject_id)
                    if subject is None:
                        error = "not_linked"
                    else:
                        report = await ingest_subject(
                            db, subject, make_client(http), storage, on_progress
                        )
                        await audit(
                            db,
                            "classroom_synced",
                            actor_id=actor.user_id,
                            actor_username=actor.username,
                            target_type="subject",
                            target_id=subject_id,
                            new_versions=report.new_versions,
                            unchanged=report.unchanged,
                            errors=report.errors,
                        )
                        await db.commit()
    except GoogleAuthError as exc:
        logger.warning("classroom_sync_auth_error", subject_id=subject_id, error=str(exc))
        error = "reconnect" if exc.invalid_grant else "google"
    except GoogleApiError as exc:
        logger.warning("classroom_sync_api_error", subject_id=subject_id, error=str(exc))
        error = "google"
    except Exception:
        logger.exception("classroom_sync_failed", subject_id=subject_id, run_id=run_id)
        error = "internal"

    try:
        await _update(
            sessions,
            run_id,
            status=(ClassroomSyncStatus.FAILED if error else ClassroomSyncStatus.DONE).value,
            error=error,
            finished_at=_now(),
        )
    except Exception:  # noqa: BLE001 — the row then reads as interrupted once stale
        logger.exception("classroom_sync_finish_failed", run_id=run_id)


def payload(run: ClassroomSyncRun, subject_name: str, now: datetime) -> dict[str, Any]:
    """What the progress panel shows for one run."""
    if is_stale(run, now):
        status, error = "interrupted", None
    else:
        status, error = run.status.lower(), run.error
    if run.assignments_total:
        part = run.submissions_done / run.submissions_total if run.submissions_total else 0.0
        percent = round(100 * (run.assignments_done + part) / run.assignments_total)
    else:
        percent = 0
    if status == "done":
        percent = 100
    return {
        "id": run.id,
        "subject_id": run.subject_id,
        "subject_name": subject_name,
        "url": f"/teacher/subjects/{run.subject_id}?tab=ops",
        "status": status,
        "error": error,
        "percent": min(percent, 100),
        "assignments_total": run.assignments_total,
        "assignments_done": run.assignments_done,
        "submissions_total": run.submissions_total,
        "submissions_done": run.submissions_done,
        "new_versions": run.new_versions,
        "errors": run.errors,
    }


async def visible_runs(db: AsyncSession, subject_ids: list[int]) -> list[dict[str, Any]]:
    """Runs for the panel: still running, or finished in the last few minutes."""
    if not subject_ids:
        return []
    now = _now()
    rows = (
        await db.execute(
            select(ClassroomSyncRun, Subject.name)
            .join(Subject, Subject.id == ClassroomSyncRun.subject_id)
            .where(
                ClassroomSyncRun.subject_id.in_(subject_ids),
                (ClassroomSyncRun.status == ClassroomSyncStatus.RUNNING.value)
                | (ClassroomSyncRun.finished_at > now - SHOW_FINISHED_FOR),
            )
            .order_by(ClassroomSyncRun.id.desc())
            .limit(5)
        )
    ).all()
    out = [payload(r, name, now) for r, name in rows]
    # An interrupted run is news for a while, then just noise.
    return [
        p
        for p, (r, _) in zip(out, rows, strict=True)
        if p["status"] != "interrupted" or now - r.heartbeat_at < STALE_AFTER + SHOW_FINISHED_FOR
    ]
