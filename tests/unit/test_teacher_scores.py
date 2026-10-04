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


def test_parse_form_reads_ints_and_skips_empty() -> None:
    crits = ts.criteria(GRADING)
    assert ts.parse_form(crits, {"score_report": " 4 ", "score_star": ""}) == {"report": 4}


@pytest.mark.parametrize("value", ["abc", "-1", "6", "2.5"])
def test_parse_form_rejects_bad_values(value: str) -> None:
    with pytest.raises(ts.ScoreError):
        ts.parse_form(ts.criteria(GRADING), {"score_report": value})


def test_is_complete_needs_required_only() -> None:
    crits = ts.criteria(GRADING)
    assert ts.is_complete(crits, {"report": 0})
    assert not ts.is_complete(crits, {"star": 3})
    assert not ts.is_complete(crits, None)


def test_compute_sums_quiz_points_and_criteria() -> None:
    b = ts.compute(GRADING, 0, 16, quiz_pct=75.0, scores={"report": 4, "star": 2})
    assert b["grade"] == 12
    assert b["mode"] == ts.MODE
    assert b["quiz_score"] == 75.0
    assert b["quiz"] == {"pct": 75.0, "points": 6, "max": 8}
    assert b["criteria"] == [
        {"key": "report", "title": "Звіт", "points": 4, "max": 5},
        {"key": "star", "title": "Зірочка", "points": 2, "max": 3},
    ]


def test_compute_rounds_half_up_and_missing_optional_is_zero() -> None:
    # 56.25% of 8 = 4.5 -> 5 (half up; Python's round() would give 4)
    b = ts.compute(GRADING, 0, 16, quiz_pct=56.25, scores={"report": 5})
    assert b["quiz"]["points"] == 5
    assert b["grade"] == 10
    assert b["criteria"][1]["points"] == 0


def test_compute_round_up_for_squads() -> None:
    b = ts.compute(GRADING, 0, 16, quiz_pct=51.0, scores={"report": 0}, round_up=True)
    assert b["quiz"]["points"] == 5  # 4.08 -> 5


def test_compute_clamps_to_max() -> None:
    b = ts.compute(GRADING, 0, 16, quiz_pct=100.0, scores={"report": 5, "star": 3})
    assert b["grade"] == 16
