"""Unit tests for the squad quiz-percentage helper (``services.grading``)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from submissions_checker.services.grading import squad_quiz_pct


def test_squad_quiz_pct_is_mean_of_each_members_passed_attempt() -> None:
    a1 = SimpleNamespace(student_id=1, is_passed=True, score=7, max_score=8)
    a2 = SimpleNamespace(student_id=2, is_passed=True, score=5, max_score=7)
    a2_old = SimpleNamespace(student_id=2, is_passed=False, score=2, max_score=7)
    pct, members = squad_quiz_pct([a1, a2_old, a2])
    assert pct == pytest.approx((87.5 + 71.428571) / 2, abs=0.01)
    assert [m["student_id"] for m in members] == [1, 2]


def test_squad_quiz_pct_none_without_passed_attempts() -> None:
    assert squad_quiz_pct([]) == (None, [])
