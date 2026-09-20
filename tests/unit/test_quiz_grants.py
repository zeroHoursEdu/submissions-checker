"""Pure helpers of services.quiz_grants — no DB."""

from __future__ import annotations

from types import SimpleNamespace

from submissions_checker.db.models.enums import QuizAttemptStatus
from submissions_checker.services.quiz_grants import (
    EXTRA_KEY,
    effective_max_attempts,
    extra_attempts,
    is_exhausted,
)


def _sub(meta):
    return SimpleNamespace(source_metadata=meta)


def _attempt(status=QuizAttemptStatus.COMPLETED, is_passed=False):
    return SimpleNamespace(status=status, is_passed=is_passed)


def test_extra_attempts_defaults_to_zero() -> None:
    assert extra_attempts(_sub({}), 7) == 0
    assert extra_attempts(_sub(None), 7) == 0
    assert extra_attempts(_sub({EXTRA_KEY: {}}), 7) == 0


def test_extra_attempts_reads_string_key() -> None:
    # JSON object keys are strings; the student id is an int in Python.
    assert extra_attempts(_sub({EXTRA_KEY: {"7": 2}}), 7) == 2
    assert extra_attempts(_sub({EXTRA_KEY: {"7": 2}}), 8) == 0


def test_effective_max_adds_extra_and_keeps_none() -> None:
    sub = _sub({EXTRA_KEY: {"7": 1}})
    assert effective_max_attempts(3, sub, 7) == 4
    assert effective_max_attempts(3, sub, 8) == 3
    assert effective_max_attempts(None, sub, 7) is None


def test_is_exhausted_requires_cap_and_only_failed_terminal_attempts() -> None:
    assert not is_exhausted([], 1)  # nothing used yet
    assert not is_exhausted([_attempt()], None)  # no cap → never exhausted
    assert is_exhausted([_attempt()], 1)
    assert not is_exhausted([_attempt()], 2)
    assert not is_exhausted([_attempt(is_passed=True)], 1)  # passed: nothing to grant
    assert not is_exhausted([_attempt(status=QuizAttemptStatus.IN_PROGRESS)], 1)
    assert is_exhausted([_attempt(status=QuizAttemptStatus.VIOLATION_FAIL)], 1)
    assert is_exhausted([_attempt(status=QuizAttemptStatus.TIMED_OUT), _attempt()], 2)
