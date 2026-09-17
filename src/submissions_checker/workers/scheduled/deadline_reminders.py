"""Scheduled job: enqueue DEADLINE_REMINDER emails for unsubmitted work.

The email task, template and outbox branch existed since the notification
system shipped; nothing ever produced the event. This walks every active
assignment whose deadline falls within the next ``deadline_reminder_days_before``
days and enqueues one reminder per enrolled real student who has no
submission yet and has not opted out. Dedup is by the outbox itself: any
DEADLINE_REMINDER row for the same (student, assignment) means it was queued.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import (
    NotificationPreference,
    OutboxMessage,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import (
    EntityType,
    NotificationCase,
    NotificationMethod,
    OutboxEventType,
    OutboxMessageState,
    SubjectStatus,
)
from submissions_checker.db.session import get_session

logger = get_logger(__name__)

# PostgreSQL advisory lock id for this job (distinct from outbox 7919, teacher digest 7927,
# migrations 7933, subject stats 7935)
DEADLINE_REMINDERS_LOCK_ID = 7937


async def enqueue_due_deadline_reminders(
    db: AsyncSession, *, now: datetime, days_before: int
) -> int:
    """Enqueue reminders for assignments due within *days_before* days. Returns the count."""
    horizon = now + timedelta(days=days_before)
    due = await db.execute(
        select(SubjectsAssignment)
        .join(Subject, Subject.id == SubjectsAssignment.subject_id)
        .where(
            Subject.status == SubjectStatus.ACTIVE,
            SubjectsAssignment.deadline.is_not(None),
            SubjectsAssignment.deadline > now,
            SubjectsAssignment.deadline <= horizon,
        )
    )
    enqueued = 0
    for sa in due.scalars():
        has_submission = (
            exists()
            .where(Submission.students_assignment_id == StudentAssignment.id)
            .correlate(StudentAssignment)
        )
        already_queued = (
            exists()
            .where(
                OutboxMessage.event_type == OutboxEventType.DEADLINE_REMINDER,
                OutboxMessage.payload["student_id"].as_integer() == Student.id,
                OutboxMessage.payload["subjects_assignment_id"].as_integer() == sa.id,
            )
            .correlate(Student)
        )
        opted_out = (
            exists()
            .where(
                NotificationPreference.student_id == Student.id,
                NotificationPreference.case == NotificationCase.DEADLINE_REMINDER,
                NotificationPreference.method == NotificationMethod.EMAIL,
                NotificationPreference.enabled.is_(False),
            )
            .correlate(Student)
        )
        students = await db.execute(
            select(Student.id)
            .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
            .outerjoin(
                StudentAssignment,
                and_(
                    StudentAssignment.student_id == Student.id,
                    StudentAssignment.subjects_assignment_id == sa.id,
                ),
            )
            .where(
                SubjectsStudents.subject_id == sa.subject_id,
                Student.type == EntityType.REAL,
                ~has_submission,
                ~already_queued,
                ~opted_out,
            )
        )
        deadline_str = sa.deadline.strftime("%Y-%m-%d %H:%M") if sa.deadline else ""
        for (student_id,) in students:
            db.add(
                OutboxMessage(
                    event_type=OutboxEventType.DEADLINE_REMINDER,
                    state=OutboxMessageState.PENDING,
                    payload={
                        "student_id": student_id,
                        "subjects_assignment_id": sa.id,
                        "deadline_str": deadline_str,
                    },
                )
            )
            enqueued += 1
    await db.commit()
    return enqueued


async def run_deadline_reminders() -> None:
    """Scheduled entry point: one replica at a time, never raises."""
    settings = get_settings()
    try:
        async with get_session() as db:
            lock_result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": DEADLINE_REMINDERS_LOCK_ID},
            )
            if not lock_result.scalar():
                logger.info("deadline_reminders_lock_not_acquired")
                return
            try:
                count = await enqueue_due_deadline_reminders(
                    db,
                    now=datetime.now(UTC),
                    days_before=settings.deadline_reminder_days_before,
                )
                logger.info("deadline_reminders_enqueued", count=count)
            finally:
                await db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": DEADLINE_REMINDERS_LOCK_ID},
                )
    except Exception as exc:  # noqa: BLE001 — matches the other jobs' top-level catch
        logger.error("deadline_reminders_error", error=str(exc))
