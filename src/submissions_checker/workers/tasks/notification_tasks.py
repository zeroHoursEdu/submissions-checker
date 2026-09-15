"""Outbox task handlers for student/teacher notification events."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import NotificationCase, NotificationMethod
from submissions_checker.db.models.feedback_token import FeedbackToken
from submissions_checker.db.models.notification_preference import NotificationPreference
from submissions_checker.db.models.quiz_template import QuizAttempt
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.db.models.user import User
from submissions_checker.services.notification_service import push_notification
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import (
    deadline_reminder_template,
    feedback_request_template,
    quiz_dispute_resolved_template,
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


async def execute_feedback_request_task(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Email a student their personal feedback link.

    Payload: feedback_token_id
    """
    from submissions_checker.db.models.feedback_request import FeedbackRequest
    from submissions_checker.db.models.semester import Semester

    settings = get_settings()
    token_id: int = payload["feedback_token_id"]

    token_result = await db.execute(select(FeedbackToken).where(FeedbackToken.id == token_id))
    token = token_result.scalar_one_or_none()
    if token is None:
        logger.warning("feedback_request_task_token_not_found", token_id=token_id)
        return

    student_result = await db.execute(select(Student).where(Student.id == token.student_id))
    student = student_result.scalar_one_or_none()
    if student is None:
        logger.warning("feedback_request_task_student_not_found", student_id=token.student_id)
        return

    if not await _is_email_enabled(db, student.id, NotificationCase.FEEDBACK_REQUEST):
        logger.info("feedback_request_email_suppressed", token_id=token_id, student_id=student.id)
        return

    fr_result = await db.execute(
        select(FeedbackRequest).where(FeedbackRequest.id == token.feedback_request_id)
    )
    feedback_request = fr_result.scalar_one_or_none()
    if feedback_request is None:
        logger.warning(
            "feedback_request_task_request_not_found", feedback_request_id=token.feedback_request_id
        )
        return

    subject_result = await db.execute(
        select(Subject).where(Subject.id == feedback_request.subject_id)
    )
    subject = subject_result.scalar_one_or_none()
    if subject is None:
        logger.warning(
            "feedback_request_task_subject_not_found", subject_id=feedback_request.subject_id
        )
        return

    semester_result = await db.execute(
        select(Semester).where(Semester.id == feedback_request.semester_id)
    )
    semester = semester_result.scalar_one_or_none()
    if semester is None:
        logger.warning(
            "feedback_request_task_semester_not_found", semester_id=feedback_request.semester_id
        )
        return

    feedback_url = f"{settings.app_base_url.rstrip('/')}/feedback/{token.token}"
    email_subject, body = feedback_request_template(
        full_name=student.full_name,
        subject_name=subject.name,
        semester_name=semester.name,
        feedback_url=feedback_url,
    )

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("feedback_request_task_no_channel", student_email=student.email)
        return

    await dispatcher.notify(student.email, email_subject, body)
    logger.info("feedback_request_email_sent", token_id=token_id, student_email=student.email)


async def execute_submission_reviewed_task(db: AsyncSession, payload: dict[str, Any]) -> None:
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
            selectinload(Submission.students_assignment).selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ),
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

    # In-app notification is a separate channel from email — pushed regardless of the
    # student's SUBMISSION_CHECKED/EMAIL preference, which only governs email.
    verb = "approved" if action == "approve" else "rejected"
    in_app_body = f'Your submission for "{assignment.title}" was {verb}.'
    if reason and action == "reject":
        in_app_body += f" Feedback: {reason}"
    notify_user_id = await db.scalar(select(User.id).where(User.student_id == student.id))
    if notify_user_id is not None:
        await push_notification(
            db,
            notify_user_id,
            email_subject,
            in_app_body,
            f"/portal/subjects/{assignment.subject_id}/assignments/{sa.id}",
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


async def execute_quiz_result_task(db: AsyncSession, payload: dict[str, Any]) -> None:
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
            selectinload(Submission.students_assignment).selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.warning("quiz_result_task_submission_not_found", submission_id=submission_id)
        return

    sa = submission.students_assignment
    student = sa.student
    assignment = sa.subjects_assignment

    portal_url = f"{settings.app_base_url.rstrip('/')}/portal/quiz/{attempt_id}/result"

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


async def execute_deadline_reminder_task(db: AsyncSession, payload: dict[str, Any]) -> None:
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
            selectinload(StudentAssignment.subjects_assignment).selectinload(
                SubjectsAssignment.subject
            ),
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


async def _resolve_review_recipients(db: AsyncSession, submission_id: int) -> list[User]:
    """Teachers to notify for a submission awaiting review.

    Primary recipient is the owner of the submission's subject. For ownerless
    (startup-loaded) subjects, fall back to all active admin accounts so the
    notification is not silently dropped.
    """
    owner_result = await db.execute(
        select(Subject.owner_id)
        .select_from(Submission)
        .join(StudentAssignment, Submission.students_assignment_id == StudentAssignment.id)
        .join(SubjectsAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id)
        .join(Subject, SubjectsAssignment.subject_id == Subject.id)
        .where(Submission.id == submission_id)
    )
    owner_id = owner_result.scalar_one_or_none()

    if owner_id is not None:
        owner = await db.execute(select(User).where(User.id == owner_id, User.is_active.is_(True)))
        user = owner.scalar_one_or_none()
        if user is not None:
            return [user]

    admins = await db.execute(select(User).where(User.role == "ADMIN", User.is_active.is_(True)))
    return list(admins.scalars().all())


async def enqueue_teacher_review_notification(db: AsyncSession, submission_id: int) -> None:
    """Enqueue a pending teacher-review notification for each responsible teacher.

    Idempotent per (teacher, submission) via ON CONFLICT DO NOTHING. Runs in the
    caller's transaction so the enqueue is atomic with the status change.
    """
    recipients = await _resolve_review_recipients(db, submission_id)
    if not recipients:
        logger.warning("teacher_review_enqueue_no_recipients", submission_id=submission_id)
        return

    for teacher in recipients:
        await db.execute(
            pg_insert(TeacherNotificationQueue)
            .values(teacher_id=teacher.id, submission_id=submission_id)
            .on_conflict_do_nothing(constraint="uq_teacher_notification_queue")
        )

    logger.info(
        "teacher_review_enqueued",
        submission_id=submission_id,
        teacher_count=len(recipients),
    )


async def execute_new_submission_task(db: AsyncSession, payload: dict[str, Any]) -> None:
    """DEPRECATED no-op handler.

    Teacher review notifications now go through enqueue_teacher_review_notification +
    the teacher_digest_processor flush job. The NEW_SUBMISSION outbox event is no longer
    emitted; this handler stays so the dispatch branch remains harmless for any stray row.
    """
    logger.info("new_submission_task_noop", payload=payload)


async def push_dispute_notifications(
    db: AsyncSession,
    submission_id: int,
    *,
    dispute_id: int,
    question_text: str,
    student_name: str,
) -> None:
    """In-app notify every teacher responsible for a submission that a question was reported.

    Goes straight to the notification bell rather than through the outbox or the review
    digest: the digest is keyed ``(teacher_id, submission_id)`` and its body says
    "submissions awaiting review", so a dispute riding along would be coalesced away and
    mislabelled. Caller owns the transaction.
    """
    recipients = await _resolve_review_recipients(db, submission_id)
    if not recipients:
        logger.warning("dispute_notify_no_recipients", submission_id=submission_id)
        return

    excerpt = question_text if len(question_text) <= 120 else f"{question_text[:117]}..."
    for teacher in recipients:
        await push_notification(
            db,
            teacher.id,
            "Quiz question reported",
            f"{student_name} reported a question as incorrect: {excerpt}",
            link=f"/teacher/disputes/{dispute_id}",
        )

    logger.info(
        "dispute_notifications_pushed",
        dispute_id=dispute_id,
        submission_id=submission_id,
        teacher_count=len(recipients),
    )


async def execute_quiz_dispute_resolved_task(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Email a student the outcome of a reported quiz question.

    The in-app notification was already pushed synchronously when the teacher ruled, so the
    bell is right immediately; this handler only owns the email. Payload: attempt_id,
    question_id, decision ('accept'|'reject'|'regraded'), note, score, max_score, is_passed.
    """
    settings = get_settings()
    attempt_id: int = payload["attempt_id"]
    decision: str = payload.get("decision", "regraded")
    note: str = payload.get("note", "")
    question_id: int = payload.get("question_id", -1)

    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        logger.warning("quiz_dispute_resolved_attempt_not_found", attempt_id=attempt_id)
        return

    result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(
            selectinload(Submission.students_assignment).selectinload(StudentAssignment.student),
            selectinload(Submission.students_assignment).selectinload(
                StudentAssignment.subjects_assignment
            ),
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.warning("quiz_dispute_resolved_submission_not_found", attempt_id=attempt_id)
        return

    sa = submission.students_assignment
    student = sa.student
    assignment = sa.subjects_assignment

    question_text = next(
        (
            str(q.get("text", ""))
            for q in (attempt.questions_snapshot or [])
            if q.get("id") == question_id
        ),
        f"#{question_id}",
    )
    portal_url = f"{settings.app_base_url.rstrip('/')}/portal/quiz/{attempt_id}/result"

    email_subject, body = quiz_dispute_resolved_template(
        full_name=student.full_name,
        assignment_title=assignment.title,
        question_text=question_text,
        decision=decision,
        note=note,
        score=payload.get("score"),
        max_score=payload.get("max_score"),
        is_passed=payload.get("is_passed"),
        portal_url=portal_url,
    )

    if not await _is_email_enabled(db, student.id, NotificationCase.SUBMISSION_CHECKED):
        logger.info(
            "quiz_dispute_resolved_email_suppressed",
            attempt_id=attempt_id,
            student_id=student.id,
        )
        return

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning("quiz_dispute_resolved_no_channel", student_email=student.email)
        return

    await dispatcher.notify(student.email, email_subject, body)
    logger.info(
        "quiz_dispute_resolved_email_sent",
        attempt_id=attempt_id,
        student_email=student.email,
        decision=decision,
    )
