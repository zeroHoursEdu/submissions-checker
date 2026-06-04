from __future__ import annotations

from typing import TYPE_CHECKING

from submissions_checker.db.models.enums import SubmissionStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.submission import Submission

_TRANSITIONS: dict[SubmissionStatus, dict[str, SubmissionStatus]] = {
    # ── New precise flow ──────────────────────────────────────────────────────
    SubmissionStatus.PENDING: {
        "start_validation": SubmissionStatus.VALIDATING,
        # Legacy: kept so existing code paths don't crash during migration
        "start_check": SubmissionStatus.CHECKING,
    },
    SubmissionStatus.VALIDATING: {
        "validation_passed": SubmissionStatus.TESTING,
        "validation_failed": SubmissionStatus.VALIDATION_FAILED,
    },
    SubmissionStatus.TESTING: {
        "test_failed": SubmissionStatus.TEST_FAILED,
        "test_passed_tests_only": SubmissionStatus.COMPLETED,
        "test_passed_ai": SubmissionStatus.AWAITING_AI_REVIEW,
        "test_passed_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
        "test_passed_quiz": SubmissionStatus.QUIZ_SENT,
    },
    SubmissionStatus.AWAITING_AI_REVIEW: {
        "start_ai_review": SubmissionStatus.AI_REVIEWING,
    },
    SubmissionStatus.AI_REVIEWING: {
        "ai_review_done_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
        "ai_review_done_completed": SubmissionStatus.COMPLETED,
        "ai_review_failed": SubmissionStatus.AI_REVIEW_FAILED,
    },
    SubmissionStatus.AI_REVIEW_FAILED: {
        "retry_ai_review": SubmissionStatus.AI_REVIEWING,
    },
    SubmissionStatus.AWAITING_TEACHER_REVIEW: {
        "teacher_approve": SubmissionStatus.COMPLETED,
        "teacher_reject": SubmissionStatus.FAILED,
        "teacher_send_quiz": SubmissionStatus.QUIZ_SENT,
    },
    # ── Legacy flow (kept for backward compat with existing data) ─────────────
    SubmissionStatus.CHECKING: {
        "check_passed_quiz":           SubmissionStatus.QUIZ_SENT,
        "check_passed_teacher_review": SubmissionStatus.WAITING_FOR_TEACHER_REVIEW,
        "check_passed_none":           SubmissionStatus.COMPLETED,
        "check_failed":                SubmissionStatus.CHECK_FAILED,
    },
    SubmissionStatus.WAITING_FOR_TEACHER_REVIEW: {
        "teacher_approve_quiz":  SubmissionStatus.QUIZ_SENT,
        "teacher_approve_done":  SubmissionStatus.COMPLETED,
        "teacher_reject":        SubmissionStatus.CHECK_FAILED,
    },
}


class InvalidTransitionError(Exception):
    pass


def transition(submission: Submission, event: str) -> None:
    """Apply event to submission, mutating its status in place.

    Raises InvalidTransitionError if the event is not valid from the current status.
    Does NOT commit — caller owns the transaction.
    """
    allowed = _TRANSITIONS.get(submission.status, {})
    next_status = allowed.get(event)
    if next_status is None:
        raise InvalidTransitionError(
            f"No transition for event={event!r} from status={submission.status!r}"
        )
    submission.status = next_status
