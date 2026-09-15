"""Recompute the gauges that come from the database.

Runs on every replica: the numbers are identical, dashboards read them with max(). The
queries are cheap counts; anything that would need a join per student is not a metric.
The refresh doubles as the database health signal — app_db_healthy drops to 0 the moment
the queries fail, which is what the DbUnhealthy alert watches.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core import metrics
from submissions_checker.core.database import get_engine, get_session_factory
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAttempt,
    QuizQuestionDispute,
    Student,
    Submission,
    User,
    UserLogin,
)
from submissions_checker.db.models.enums import (
    EntityType,
    OutboxMessageState,
    QuizAttemptStatus,
    QuizDisputeStatus,
    SubmissionStatus,
    UserRole,
)

logger = get_logger(__name__)

_WINDOWS = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int((await db.execute(stmt)).scalar_one() or 0)


async def compute_gauges(db: AsyncSession) -> dict[str, float]:
    """Run every gauge query in the given session. Raises on any database error."""
    values: dict[str, float] = {}
    values["students_total"] = await _count(
        db, select(func.count(Student.id)).where(Student.type == EntityType.REAL)
    )
    for name, span in _WINDOWS.items():
        values[f"students_active_{name}"] = await _count(
            db,
            select(func.count(func.distinct(User.student_id)))
            .select_from(UserLogin)
            .join(User, User.id == UserLogin.user_id)
            .where(
                User.role == UserRole.STUDENT,
                User.student_id.is_not(None),
                UserLogin.logged_in_at > func.now() - span,
            ),
        )
    values["quiz_attempts_in_progress"] = await _count(
        db,
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.status == QuizAttemptStatus.IN_PROGRESS
        ),
    )
    values["submissions_awaiting_teacher_review"] = await _count(
        db,
        select(func.count(Submission.id)).where(
            Submission.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
        ),
    )
    values["disputes_open"] = await _count(
        db,
        select(func.count(QuizQuestionDispute.id)).where(
            QuizQuestionDispute.status == QuizDisputeStatus.OPEN
        ),
    )
    values["outbox_pending"] = await _count(
        db,
        select(func.count(OutboxMessage.id)).where(
            OutboxMessage.state == OutboxMessageState.PENDING
        ),
    )
    values["outbox_error"] = await _count(
        db,
        select(func.count(OutboxMessage.id)).where(OutboxMessage.state == OutboxMessageState.ERROR),
    )
    oldest = (
        await db.execute(
            select(func.extract("epoch", func.now() - func.min(OutboxMessage.created_at))).where(
                OutboxMessage.state == OutboxMessageState.PENDING
            )
        )
    ).scalar_one_or_none()
    values["outbox_oldest_pending_age_seconds"] = max(float(oldest or 0), 0.0)
    return values


def _apply(values: dict[str, float]) -> None:
    metrics.students_total.set(values["students_total"])
    for name in _WINDOWS:
        metrics.students_active.labels(window=name).set(values[f"students_active_{name}"])
    metrics.quiz_attempts_in_progress.set(values["quiz_attempts_in_progress"])
    metrics.submissions_awaiting_teacher_review.set(values["submissions_awaiting_teacher_review"])
    metrics.disputes_open.set(values["disputes_open"])
    metrics.outbox_pending.set(values["outbox_pending"])
    metrics.outbox_error.set(values["outbox_error"])
    metrics.outbox_oldest_pending_age_seconds.set(values["outbox_oldest_pending_age_seconds"])


def _apply_pool() -> None:
    # The async engine's pool is an AsyncAdaptedQueuePool; the base Pool type has no
    # size/overflow accessors, so read them dynamically.
    pool: Any = get_engine().pool
    metrics.db_pool_checked_out.set(pool.checkedout())
    # SQLAlchemy reports overflow as negative while the pool is below its base size.
    metrics.db_pool_size.set(pool.size() + max(pool.overflow(), 0))


async def refresh_metrics() -> None:
    """Scheduled entry point. Never raises: a failing refresh is itself the signal."""
    try:
        _apply_pool()
        async with get_session_factory()() as db:
            values = await compute_gauges(db)
        _apply(values)
        metrics.app_db_healthy.set(1)
    except Exception as exc:  # noqa: BLE001 — any failure means "cannot read the database"
        metrics.app_db_healthy.set(0)
        logger.error("metrics_refresh_failed", error=str(exc))
