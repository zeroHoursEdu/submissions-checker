"""Scheduled job: precompute the four Панель stat-card numbers per subject.

Runs on subject_stats_refresh_interval (default 300s). The teacher_subject
route reads subject_gradebook_stats live but never computes these numbers
itself — see services.gradebook.compute_cached_stats for the calculation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import SubjectStatus
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
from submissions_checker.db.session import get_session
from submissions_checker.services.gradebook import (
    compute_cached_stats,
    fetch_integrity_rows,
    fetch_roster_rows,
)

logger = get_logger(__name__)

# PostgreSQL advisory lock id for this job (distinct from outbox 7919, teacher digest 7927,
# migrations 7933)
SUBJECT_STATS_REFRESH_LOCK_ID = 7935


async def refresh_subject_gradebook_stats() -> None:
    """Recompute and upsert subject_gradebook_stats for every active subject."""
    try:
        async with get_session() as db:
            lock_result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": SUBJECT_STATS_REFRESH_LOCK_ID},
            )
            if not lock_result.scalar():
                logger.info("subject_stats_refresh_lock_not_acquired")
                return

            try:
                subject_ids = (
                    (
                        await db.execute(
                            select(Subject.id).where(Subject.status == SubjectStatus.ACTIVE)
                        )
                    )
                    .scalars()
                    .all()
                )
                now = datetime.now(UTC)
                for subject_id in subject_ids:
                    roster_rows = await fetch_roster_rows(db, subject_id)
                    integrity_rows = await fetch_integrity_rows(db, subject_id)
                    stats = compute_cached_stats(roster_rows, integrity_rows, now=now)

                    existing = await db.get(SubjectGradebookStats, subject_id)
                    if existing is None:
                        db.add(
                            SubjectGradebookStats(
                                subject_id=subject_id,
                                pending_review_count=stats.pending_review_count,
                                average_mark_pct=stats.average_mark_pct,
                                pass_pct=stats.pass_pct,
                                cheating_pct=stats.cheating_pct,
                                computed_at=now,
                            )
                        )
                    else:
                        existing.pending_review_count = stats.pending_review_count
                        existing.average_mark_pct = stats.average_mark_pct
                        existing.pass_pct = stats.pass_pct
                        existing.cheating_pct = stats.cheating_pct
                        existing.computed_at = now
                    await db.commit()

                logger.info("subject_stats_refresh_completed", subjects=len(subject_ids))

            finally:
                await db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": SUBJECT_STATS_REFRESH_LOCK_ID},
                )

    except Exception as exc:  # noqa: BLE001 — matches teacher_digest_processor's top-level catch
        logger.error("subject_stats_refresh_error", error=str(exc))
