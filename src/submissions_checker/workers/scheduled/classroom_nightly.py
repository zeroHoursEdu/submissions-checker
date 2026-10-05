"""Scheduled job: nightly Classroom ingest for every linked subject, then LLM grading.

Cron at ``llm_grading_start_hour`` in ``llm_grading_timezone``, registered only when
Classroom is configured. One replica wins the advisory lock (held on a dedicated
connection for the whole run); the other one, and the teacher's "sync now" button, see
it taken and back off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from submissions_checker.core.config import Settings, get_settings
from submissions_checker.core.database import get_engine, get_session_factory
from submissions_checker.core.logging import get_logger
from submissions_checker.core.metrics import classroom_sync_total
from submissions_checker.db.models.enums import GoogleConnectionStatus
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.subject import Subject
from submissions_checker.services.google.client import ClassroomClient
from submissions_checker.services.google.crypto import decrypt_token
from submissions_checker.services.google.ingest import classroom_lock, ingest_subject
from submissions_checker.services.google.oauth import GoogleAuthError
from submissions_checker.services.llm_grading.judge import get_judge
from submissions_checker.services.llm_grading.runner import grading_loop, reap_stale
from submissions_checker.services.storage import StorageService, get_storage

logger = get_logger(__name__)

GOOGLE_TIMEOUT = 20.0


def _now() -> datetime:
    return datetime.now(UTC)


async def _ingest_all(
    db_factory: async_sessionmaker[AsyncSession], settings: Settings, storage: StorageService
) -> None:
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(Subject.id, GoogleConnection.refresh_token_enc)
                .join(GoogleConnection, GoogleConnection.id == Subject.classroom_connection_id)
                .where(
                    Subject.classroom_course_id.is_not(None),
                    GoogleConnection.status == GoogleConnectionStatus.ACTIVE.value,
                )
                .order_by(Subject.id)
            )
        ).all()

    async with httpx.AsyncClient(timeout=GOOGLE_TIMEOUT) as http:
        for subject_id, token_enc in rows:
            started = _now()
            try:
                async with db_factory() as db:
                    subject = await db.get(Subject, subject_id)
                    if subject is None:
                        continue
                    client = ClassroomClient(settings, decrypt_token(settings, token_enc), http)
                    report = await ingest_subject(db, subject, client, storage)
            except GoogleAuthError as exc:
                outcome = "reconnect" if exc.invalid_grant else "auth_error"
                classroom_sync_total.labels(outcome=outcome).inc()
                logger.warning("classroom_ingest_auth_error", subject_id=subject_id, error=str(exc))
                continue
            except Exception:  # noqa: BLE001 — one subject must not stop the others
                classroom_sync_total.labels(outcome="error").inc()
                logger.exception("classroom_ingest_subject_failed", subject_id=subject_id)
                continue
            classroom_sync_total.labels(outcome="partial" if report.errors else "ok").inc()
            logger.info(
                "classroom_ingest_subject_done",
                subject_id=subject_id,
                new_versions=report.new_versions,
                errors=report.errors,
                duration_s=round((_now() - started).total_seconds(), 1),
            )


async def run_classroom_nightly() -> None:
    try:
        await _run()
    except Exception:  # noqa: BLE001 — matches the other scheduled jobs' top-level catch
        logger.exception("classroom_nightly_error")


async def _run() -> None:
    settings = get_settings()
    db_factory = get_session_factory()
    async with classroom_lock(get_engine()) as locked:
        if not locked:
            logger.info("classroom_nightly_locked")
            return

        async with db_factory() as db:
            await reap_stale(db, _now())

        storage = get_storage(settings)
        if storage is None:
            logger.warning("classroom_nightly_no_storage")
            return

        await _ingest_all(db_factory, settings, storage)

        judge = get_judge(settings)
        try:
            graded = await grading_loop(
                db_factory,
                judge,
                storage,
                now_fn=_now,
                end_hour=settings.llm_grading_end_hour,
                cap=settings.llm_grading_nightly_cap,
                tz=ZoneInfo(settings.llm_grading_timezone),
            )
        finally:
            await judge.aclose()
        logger.info("classroom_nightly_done", graded=graded)
