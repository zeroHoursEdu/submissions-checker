"""Outbox task handlers for student/teacher notification events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import NotificationCase, NotificationMethod
from submissions_checker.db.models.notification_preference import NotificationPreference
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.user import User
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import (
    deadline_reminder_template,
    new_submission_template,
    quiz_result_template,
    submission_reviewed_template,
)

logger = get_logger(__name__)


async def _is_email_enabled(db: AsyncSession, student_id: int, case: NotificationCase) -> bool:
    """Return True if the student has email notifications enabled for the given case.

    Missing row is treated as enabled (opt-out model).
    """
    result = await db.execute(
        select(NotificationPreference).where(
            NotificationPreference.student_id == student_id,
            NotificationPreference.case == case,
            NotificationPreference.method == NotificationMethod.EMAIL,
        )
    )
    pref = result.scalar_one_or_none()
    return pref is None or pref.enabled


async def execute_submission_reviewed_task(db: AsyncSession, payload: dict) -> None:
    """Email student when their submission is approved or rejected by a teacher.

    Payload: submission_id, action ('approve'|'reject'), reason
    """
    settings = get_settings()
    submission_id: int = payload["submission_id"]
    action: str = payload["action"]
    reason: str = payload.get("reason", "")

    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.warning("submission_reviewed_task_submission_not_found", submission_id=submission_id)
        return

    sa = submission.students_assignment
    student = sa.student
    assignment = sa.subjects_assignment

    portal_url = (
        f"{settings.app_base_url.rstrip('/')}"
        f"/portal/subjects/{assignment.subject_id}/assignments/{sa.id}"
    )

    email_subject, body = submission_reviewed_template(
        full_name=student.full_name,
        assignment_title=assignment.title,
        action=action,
        reason=reason,
        portal_url=portal_url,
    )

    if not await _is_email_enabled(db, student.id, NotificationCase.SUBMISSION_CHECKED):
        logger.info(
            "submission_reviewed_email_suppressed",
            submission_id=submission_id,
            student_id=student.id,
        )
        return

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("submission_reviewed_task_no_channel", student_email=student.email)
        return

    await dispatcher.notify(student.email, email_subject, body)
    logger.info(
        "submission_reviewed_email_sent",
        submission_id=submission_id,
        student_email=student.email,
        action=action,
    )


async def execute_quiz_result_task(db: AsyncSession, payload: dict) -> None:
    """Email student their quiz result after completing a quiz attempt.

    Payload: submission_id, score, max_score, is_passed, attempts_left, attempt_id
    """
    settings = get_settings()
    submission_id: int = payload["submission_id"]
    score: int = payload["score"]
    max_score: int = payload["max_score"]
    is_passed: bool = payload["is_passed"]
    attempts_left: int | None = payload.get("attempts_left")
    attempt_id: int = payload["attempt_id"]

    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.warning("quiz_result_task_submission_not_found", submission_id=submission_id)
        return

    sa = submission.students_assignment
    student = sa.student
    assignment = sa.subjects_assignment

    portal_url = (
        f"{settings.app_base_url.rstrip('/')}"
        f"/portal/quiz/{attempt_id}/result"
    )

    email_subject, body = quiz_result_template(
        full_name=student.full_name,
        assignment_title=assignment.title,
        score=score,
        max_score=max_score,
        is_passed=is_passed,
        attempts_left=attempts_left,
        portal_url=portal_url,
    )

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("quiz_result_task_no_channel", student_email=student.email)
        return

    await dispatcher.notify(student.email, email_subject, body)
    logger.info("quiz_result_email_sent", submission_id=submission_id, attempt_id=attempt_id)


async def execute_deadline_reminder_task(db: AsyncSession, payload: dict) -> None:
    """Email student with a deadline reminder.

    Payload: student_id, subjects_assignment_id, deadline_str
    """
    settings = get_settings()
    student_id: int = payload["student_id"]
    sa_id: int = payload["subjects_assignment_id"]
    deadline_str: str = payload["deadline_str"]

    result = await db.execute(
        select(StudentAssignment)
        .where(
            StudentAssignment.student_id == student_id,
            StudentAssignment.subjects_assignment_id == sa_id,
        )
        .options(
            selectinload(StudentAssignment.student),
            selectinload(StudentAssignment.subjects_assignment)
            .selectinload(SubjectsAssignment.subject),
        )
    )
    student_assignment = result.scalar_one_or_none()
    if student_assignment is None:
        logger.warning("deadline_reminder_task_sa_not_found", student_id=student_id, sa_id=sa_id)
        return

    student = student_assignment.student
    assignment = student_assignment.subjects_assignment
    subject = assignment.subject

    portal_url = (
        f"{settings.app_base_url.rstrip('/')}"
        f"/portal/subjects/{subject.id}/assignments/{student_assignment.id}"
    )

    email_subject, body = deadline_reminder_template(
        full_name=student.full_name,
        assignment_title=assignment.title,
        subject_name=subject.name,
        deadline_str=deadline_str,
        portal_url=portal_url,
    )

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("deadline_reminder_task_no_channel", student_email=student.email)
        return

    await dispatcher.notify(student.email, email_subject, body)
    logger.info("deadline_reminder_sent", student_email=student.email, sa_id=sa_id)


async def execute_new_submission_task(db: AsyncSession, payload: dict) -> None:
    """Notify teachers that a submission is awaiting manual review.

    Payload: submission_id
    """
    settings = get_settings()
    submission_id: int = payload["submission_id"]

    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.warning("new_submission_task_not_found", submission_id=submission_id)
        return

    sa = submission.students_assignment
    student = sa.student
    assignment = sa.subjects_assignment

    review_url = (
        f"{settings.app_base_url.rstrip('/')}"
        f"/teacher/submissions/{submission_id}/review"
    )

    email_subject, body = new_submission_template(
        teacher_name="Teacher",
        student_name=student.full_name,
        assignment_title=assignment.title,
        review_url=review_url,
    )

    # Find all teacher accounts and notify each
    teachers_result = await db.execute(
        select(User).where(User.role.in_(["TEACHER", "ADMIN"]), User.is_active.is_(True))
    )
    teachers = teachers_result.scalars().all()

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("new_submission_task_no_channel", submission_id=submission_id)
        return

    for teacher in teachers:
        # Use username as fallback if teacher has no email — skip if no email-able field
        # In practice teacher accounts may not have a separate email column;
        # adapt this once teacher email storage is added.
        pass  # TODO: once teacher email field exists, send email

    logger.info("new_submission_task_done", submission_id=submission_id, teacher_count=len(teachers))
