from __future__ import annotations

from typing import TYPE_CHECKING

from submissions_checker.db.models.enums import SubmissionStatus

if TYPE_CHECKING:
    from submissions_checker.db.models.submission import Submission

_TRANSITIONS: dict[SubmissionStatus, dict[str, SubmissionStatus]] = {
    SubmissionStatus.PENDING: {
        "start_check": SubmissionStatus.CHECKING,
    },
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
