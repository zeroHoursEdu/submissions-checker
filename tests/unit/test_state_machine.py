"""Unit tests for core.state_machine — every allowed/disallowed transition.

`transition()` only needs an object with a mutable `.status`; we use a tiny stub
rather than a real ORM-mapped Submission so no DB is involved.
"""

from __future__ import annotations

import pytest

from submissions_checker.core.state_machine import (
    InvalidTransitionError,
    transition,
)
from submissions_checker.db.models.enums import SubmissionStatus as S


class _Sub:
    """Minimal stand-in for a Submission (only `.status` is touched)."""

    def __init__(self, status: S) -> None:
        self.status = status


# (from_status, event, to_status) for every edge defined in _TRANSITIONS.
LEGAL = [
    # New precise flow
    (S.PENDING, "start_validation", S.VALIDATING),
    (S.PENDING, "start_check", S.CHECKING),  # legacy alias kept on PENDING
    (S.VALIDATING, "validation_passed", S.TESTING),
    (S.VALIDATING, "validation_failed", S.VALIDATION_FAILED),
    (S.TESTING, "test_failed", S.TEST_FAILED),
    (S.TESTING, "test_passed_tests_only", S.COMPLETED),
    (S.TESTING, "test_passed_ai", S.AWAITING_AI_REVIEW),
    (S.TESTING, "test_passed_teacher", S.AWAITING_TEACHER_REVIEW),
    (S.TESTING, "test_passed_quiz", S.QUIZ_SENT),
    (S.AWAITING_AI_REVIEW, "start_ai_review", S.AI_REVIEWING),
    (S.AI_REVIEWING, "ai_review_done_teacher", S.AWAITING_TEACHER_REVIEW),
    (S.AI_REVIEWING, "ai_review_done_completed", S.COMPLETED),
    (S.AI_REVIEWING, "ai_review_passed_quiz", S.QUIZ_SENT),
    (S.AI_REVIEWING, "ai_review_failed", S.AI_REVIEW_FAILED),
    (S.AI_REVIEW_FAILED, "retry_ai_review", S.AI_REVIEWING),
    (S.AWAITING_TEACHER_REVIEW, "teacher_approve", S.COMPLETED),
    (S.AWAITING_TEACHER_REVIEW, "teacher_reject", S.FAILED),
    (S.AWAITING_TEACHER_REVIEW, "teacher_send_quiz", S.QUIZ_SENT),
    # Legacy flow
    (S.CHECKING, "check_passed_quiz", S.QUIZ_SENT),
    (S.CHECKING, "check_passed_teacher_review", S.WAITING_FOR_TEACHER_REVIEW),
    (S.CHECKING, "check_passed_none", S.COMPLETED),
    (S.CHECKING, "check_failed", S.CHECK_FAILED),
    (S.WAITING_FOR_TEACHER_REVIEW, "teacher_approve_quiz", S.QUIZ_SENT),
    (S.WAITING_FOR_TEACHER_REVIEW, "teacher_approve_done", S.COMPLETED),
    (S.WAITING_FOR_TEACHER_REVIEW, "teacher_reject", S.CHECK_FAILED),
]


@pytest.mark.parametrize("start,event,expected", LEGAL)
def test_legal_transition_succeeds(start: S, event: str, expected: S) -> None:
    sub = _Sub(start)
    transition(sub, event)
    assert sub.status == expected


# Illegal: events that are valid *somewhere* but not from this status, plus
# unknown events and terminal states.
ILLEGAL = [
    (S.PENDING, "validation_passed"),  # wrong event for PENDING
    (S.PENDING, "teacher_approve"),
    (S.VALIDATING, "start_validation"),  # cannot re-start
    (S.VALIDATING, "test_passed_ai"),
    (S.TESTING, "validation_passed"),
    (S.COMPLETED, "teacher_approve"),  # terminal
    (S.COMPLETED, "start_validation"),
    (S.FAILED, "teacher_reject"),  # terminal
    (S.VALIDATION_FAILED, "validation_passed"),
    (S.TEST_FAILED, "test_passed_ai"),
    (S.AWAITING_TEACHER_REVIEW, "teacher_approve_done"),  # legacy event on new state
    (S.CHECKING, "teacher_approve"),  # new event on legacy state
    (S.PENDING, "totally_unknown_event"),
]


@pytest.mark.parametrize("start,event", ILLEGAL)
def test_illegal_transition_raises(start: S, event: str) -> None:
    sub = _Sub(start)
    with pytest.raises(InvalidTransitionError):
        transition(sub, event)


def test_illegal_transition_does_not_mutate_status() -> None:
    sub = _Sub(S.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        transition(sub, "start_validation")
    assert sub.status == S.COMPLETED


def test_error_message_includes_event_and_status() -> None:
    sub = _Sub(S.PENDING)
    with pytest.raises(InvalidTransitionError) as exc:
        transition(sub, "nope")
    msg = str(exc.value)
    assert "nope" in msg
    assert "PENDING" in msg


def test_status_with_no_outgoing_edges_raises():
    # PROCESSING/REVIEWING exist in the enum but are not in the transition table.
    sub = _Sub(S.PROCESSING)
    with pytest.raises(InvalidTransitionError):
        transition(sub, "start_validation")
