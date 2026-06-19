"""Coalesced teacher review digest flusher (scheduled job).

Groups unsent teacher_notification_queue rows per teacher and sends a single digest
email per teacher when their batch is ready (window elapsed OR eager threshold reached).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select, text, update

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.db.models.user import User
from submissions_checker.db.session import get_session
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import teacher_digest_template

logger = get_logger(__name__)

# PostgreSQL advisory lock id for the teacher digest flusher (distinct from outbox 7919)
TEACHER_DIGEST_LOCK_ID = 7927


async def flush_teacher_digests() -> None:
    """Send coalesced review digests to teachers whose batches are ready."""
    settings = get_settings()
    if not settings.teacher_digest_enabled:
        return

    window_seconds: int = settings.teacher_digest_window_seconds
    max_batch: int = settings.teacher_digest_max_batch

    try:
        async with get_session() as db:
            lock_result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": TEACHER_DIGEST_LOCK_ID},
            )
            if not lock_result.scalar():
                logger.info("teacher_digest_lock_not_acquired")
                return

            try:
                dispatcher = build_dispatcher(settings)
                if not dispatcher._channels:
                    logger.warning("teacher_digest_no_channel")
                    return

                # Load all unsent entries with the display fields needed for the email.
                rows = (
                    await db.execute(
                        select(
                            TeacherNotificationQueue.id,
                            TeacherNotificationQueue.teacher_id,
                            TeacherNotificationQueue.submission_id,
                            TeacherNotificationQueue.created_at,
                            User.email,
                            User.username,
                            Student.full_name,
                            SubjectsAssignment.title,
                        )
                        .join(User, TeacherNotificationQueue.teacher_id == User.id)
                        .join(Submission, TeacherNotificationQueue.submission_id == Submission.id)
                        .join(
                            StudentAssignment,
                            Submission.students_assignment_id == StudentAssignment.id,
                        )
                        .join(Student, StudentAssignment.student_id == Student.id)
                        .join(
                            SubjectsAssignment,
                            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
                        )
                        .where(TeacherNotificationQueue.sent_at.is_(None))
                        .order_by(
                            TeacherNotificationQueue.teacher_id,
                            TeacherNotificationQueue.created_at.asc(),
                        )
                    )
                ).all()

                by_teacher: dict[int, list] = defaultdict(list)
                for row in rows:
                    by_teacher[row.teacher_id].append(row)

                now = datetime.now(UTC)
                base = settings.app_base_url.rstrip("/")
                dashboard_url = f"{base}/teacher"
                sent_teachers = 0

                for teacher_id, entries in by_teacher.items():
                    oldest = entries[0].created_at  # ordered created_at asc
                    age = (now - oldest).total_seconds()
                    ready = len(entries) >= max_batch or age >= window_seconds
                    if not ready:
                        continue

                    email = entries[0].email
                    if not email:
                        logger.warning(
                            "teacher_digest_no_email",
                            teacher_id=teacher_id,
                            pending=len(entries),
                        )
                        continue

                    items = [
                        (
                            e.full_name,
                            e.title,
                            f"{base}/teacher/submissions/{e.submission_id}/review",
                        )
                        for e in entries
                    ]
                    subject, body = teacher_digest_template(
                        teacher_name=entries[0].username,
                        items=items,
                        dashboard_url=dashboard_url,
                    )

                    try:
                        await dispatcher.notify(email, subject, body)
                    except Exception as exc:  # leave rows pending, retry next interval
                        logger.error(
                            "teacher_digest_send_failed",
                            teacher_id=teacher_id,
                            error=str(exc),
                        )
                        continue

                    ids = [e.id for e in entries]
                    await db.execute(
                        update(TeacherNotificationQueue)
                        .where(TeacherNotificationQueue.id.in_(ids))
                        .values(sent_at=now)
                    )
                    await db.commit()
                    sent_teachers += 1
                    logger.info(
                        "teacher_digest_sent",
                        teacher_id=teacher_id,
                        count=len(entries),
                    )

                logger.info("teacher_digest_flush_completed", teachers_emailed=sent_teachers)

            finally:
                await db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": TEACHER_DIGEST_LOCK_ID},
                )

    except Exception as exc:
        logger.error("teacher_digest_flush_error", error=str(exc))
