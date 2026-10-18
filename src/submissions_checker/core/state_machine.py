from __future__ import annotations

from typing import TYPE_CHECKING

from submissions_checker.db.models.enums import SubmissionStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.submission import Submission

_TRANSITIONS: dict[SubmissionStatus, dict[str, SubmissionStatus]] = {
    # ── New precise flow ──────────────────────────────────────────────────────
    SubmissionStatus.PENDING: {
        "start_validation": SubmissionStatus.VALIDATING,
    },
    SubmissionStatus.VALIDATING: {
        "validation_passed": SubmissionStatus.TESTING,
        "validation_failed": SubmissionStatus.VALIDATION_FAILED,
    },
    SubmissionStatus.TESTING: {
        "test_failed": SubmissionStatus.TEST_FAILED,
        "test_passed_ai": SubmissionStatus.AWAITING_AI_REVIEW,
    },
    SubmissionStatus.AWAITING_AI_REVIEW: {
        "start_ai_review": SubmissionStatus.AI_REVIEWING,
    },
    SubmissionStatus.AI_REVIEWING: {
        "ai_review_failed": SubmissionStatus.AI_REVIEW_FAILED,
    },
    SubmissionStatus.AI_REVIEW_FAILED: {
        "retry_ai_review": SubmissionStatus.AI_REVIEWING,
    },
    SubmissionStatus.QUIZ_SENT: {
        # Squads only: member A exhausted (→ FAILED), was granted an attempt (→ QUIZ_SENT),
        # and member B — mid-attempt at the time — has since exhausted too. B's grant
        # lands while the submission is already QUIZ_SENT; the self-loop keeps one path.
        "quiz_attempt_granted": SubmissionStatus.QUIZ_SENT,
    },
    SubmissionStatus.AWAITING_TEACHER_REVIEW: {
        "teacher_reject": SubmissionStatus.FAILED,
    },
    # FAILED is otherwise terminal; the exceptions are a teacher granting one more quiz
    # attempt to a student who exhausted max_quiz_attempts (services.quiz_grants checks that
    # this is why it failed) and the settle events below.
    SubmissionStatus.FAILED: {
        "quiz_attempt_granted": SubmissionStatus.QUIZ_SENT,
    },
}


# Requirements model: `settle` decides the status from what is done, so its four events are
# allowed from every settled state (and are idempotent self-loops where the status is
# unchanged). `processing_done` ends the upload pipeline; settle runs right after it.
# AI_REVIEW_FAILED is among them for an optional ai_review item: its failure does not hold
# the work back (settle leaves a required one alone).
for _status in (
    SubmissionStatus.PENDING,
    SubmissionStatus.AI_REVIEW_FAILED,
    SubmissionStatus.QUIZ_SENT,
    SubmissionStatus.AWAITING_TEACHER_REVIEW,
    SubmissionStatus.COMPLETED,
    SubmissionStatus.FAILED,
):
    _TRANSITIONS.setdefault(_status, {}).update(
        {
            "requirements_met": SubmissionStatus.COMPLETED,
            "requirement_failed": SubmissionStatus.FAILED,
            "awaiting_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
            "awaiting_quiz": SubmissionStatus.QUIZ_SENT,
        }
    )
for _status in (SubmissionStatus.TESTING, SubmissionStatus.AI_REVIEWING):
    _TRANSITIONS[_status]["processing_done"] = SubmissionStatus.AWAITING_TEACHER_REVIEW


# A teacher may send a stuck or failed submission back through the checks. Every
# state that can be reached by the pipeline (or by a worker dying mid-flight) gets
# the same exit; terminal COMPLETED is deliberately excluded. (The requirements events
# above are also defined from COMPLETED, but only services.settle may emit
# requirements_met / requirement_failed / awaiting_teacher / awaiting_quiz, and settle
# never leaves COMPLETED: it only rewrites the grade there.)
for _status in (
    SubmissionStatus.VALIDATING,
    SubmissionStatus.TESTING,
    SubmissionStatus.AWAITING_AI_REVIEW,
    SubmissionStatus.AI_REVIEWING,
    SubmissionStatus.AI_REVIEW_FAILED,
    SubmissionStatus.VALIDATION_FAILED,
    SubmissionStatus.TEST_FAILED,
    SubmissionStatus.FAILED,
    SubmissionStatus.AWAITING_TEACHER_REVIEW,
):
    _TRANSITIONS.setdefault(_status, {})["requeue_checks"] = SubmissionStatus.PENDING


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
