"""Teacher-granted extra quiz attempts.

A student who uses up ``max_quiz_attempts`` without passing sends the submission to
``FAILED``. A teacher may hand that one student one more attempt. The grant is recorded per
student in ``submissions.source_metadata["quiz_extra_attempts"]`` (so a fresh upload starts
clean and squad members are counted apart), raises the effective attempt cap everywhere the
cap is read, and moves the submission back to ``QUIZ_SENT`` through an explicit event.
Nothing here touches ``quiz_attempts`` rows — history stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from submissions_checker.db.models.enums import QuizAttemptStatus

EXTRA_KEY = "quiz_extra_attempts"

_TERMINAL = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)


class GrantError(Exception):
    """The (submission, student) pair is not stuck on an exhausted quiz."""


def extra_attempts(submission: Any, student_id: int | None) -> int:
    """Extra attempts granted to this student on this submission (0 when none)."""
    meta = submission.source_metadata or {}
    extras = meta.get(EXTRA_KEY) or {}
    return int(extras.get(str(student_id), 0))


def effective_max_attempts(
    base: int | None, submission: Any, student_id: int | None
) -> int | None:
    """The config's cap plus every grant; ``None`` stays ``None`` (no cap at all)."""
    if base is None:
        return None
    return int(base) + extra_attempts(submission, student_id)


def is_exhausted(attempts: Sequence[Any], effective_max: int | None) -> bool:
    """True when the student has used every attempt they are allowed and none passed.

    Needs at least one attempt, every attempt terminal (nothing still running), no pass,
    and a cap to exhaust — an uncapped quiz can never be "used up".
    """
    if effective_max is None or not attempts:
        return False
    if any(a.status not in _TERMINAL for a in attempts):
        return False
    if any(a.is_passed for a in attempts):
        return False
    return len(attempts) >= effective_max
