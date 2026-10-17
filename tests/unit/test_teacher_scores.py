"""Unit tests for the quiz_and_teacher_scores grade rules (``services.teacher_scores``)."""

from __future__ import annotations

from typing import Any

import pytest

from submissions_checker.services import teacher_scores as ts

GRADING: dict[str, Any] = {
    "quiz_points": 8,
    "teacher_criteria": [
        {"key": "report", "title": "Звіт", "max": 5},
        {"key": "star", "title": "Зірочка", "max": 3, "optional": True},
    ],
}
A_CFG: dict[str, Any] = {
    "review_mode": ts.MODE,
    "min_grade": 0,
    "max_grade": 16,
    "grading": GRADING,
    "quiz": {"questions": [{"text": "q"}]},
}


def _crit(key: str, mx: int) -> dict[str, Any]:
    return {"key": key, "title": key, "max": mx}


def test_is_scored_mode() -> None:
    assert ts.is_scored_mode({"review_mode": ts.MODE})
    assert not ts.is_scored_mode({"review_mode": "quiz_then_teacher"})
    assert not ts.is_scored_mode(None)


def test_criteria_parsed_with_optional_default_false() -> None:
    assert ts.criteria(GRADING) == [
        ts.Criterion("report", "Звіт", 5, False),
        ts.Criterion("star", "Зірочка", 3, True),
    ]


def test_validate_accepts_matching_total() -> None:
    ts.validate_assignment("lab3", A_CFG)


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"quiz": {}}, "quiz"),
        ({"max_grade": 17}, r"quiz_points \+ criteria"),
        ({"grading": {**GRADING, "teacher_criteria": []}}, "teacher_criteria"),
        ({"grading": {**GRADING, "quiz_points": -1}}, "quiz_points"),
        ({"grading": {**GRADING, "teacher_criteria": [_crit("Bad Key", 8)]}}, "key"),
        (
            {"grading": {**GRADING, "teacher_criteria": [_crit("a", 4), _crit("a", 4)]}},
            "duplicate",
        ),
        ({"grading": {**GRADING, "teacher_criteria": [_crit("a", 0)]}}, "max"),
        ({"grading": {**GRADING, "teacher_criteria": [{"key": "a", "max": True}]}}, "max"),
    ],
)
def test_validate_rejects(patch: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        ts.validate_assignment("lab3", {**A_CFG, **patch})
