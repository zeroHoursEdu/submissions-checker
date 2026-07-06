"""Unit tests for the pure weighted-grade calculation (``services.grading``)."""

from __future__ import annotations

from submissions_checker.services.grading import compute_grade


def test_full_blend_of_works_quality_and_quiz() -> None:
    # code = 0.7*90 + 0.3*80 = 87 ; grade = 0.6*87 + 0.4*50 = 72.2 → 72
    cfg = {
        "code_weight": 0.6,
        "quiz_weight": 0.4,
        "code": {"works_weight": 0.7, "quality_weight": 0.3},
    }
    b = compute_grade(cfg, 0, 100, works_pct=90.0, ai_mark=80.0, quiz_pct=50.0)
    assert b.grade == 72
    assert b.works_score == 90.0
    assert b.quality_score == 80.0
    assert b.quiz_score == 50.0


def test_missing_quiz_renormalizes_to_code_only() -> None:
    cfg = {"code_weight": 0.6, "quiz_weight": 0.4, "code": {"works_weight": 1.0}}
    # quiz absent → grade is the code score alone (works only) = 90.
    b = compute_grade(cfg, 0, 100, works_pct=90.0, ai_mark=None, quiz_pct=None)
    assert b.grade == 90
    assert b.quiz_score is None


def test_missing_ai_mark_renormalizes_code_to_works_only() -> None:
    cfg = {"code_weight": 1.0, "code": {"works_weight": 0.7, "quality_weight": 0.3}}
    # quality absent → code score is works alone = 80.
    b = compute_grade(cfg, 0, 100, works_pct=80.0, ai_mark=None, quiz_pct=None)
    assert b.grade == 80
    assert b.quality_score is None


def test_grade_scaled_and_clamped_to_range() -> None:
    # 50% into a 40..80 band → 40 + 0.5*40 = 60.
    cfg = {"code_weight": 1.0, "code": {"works_weight": 1.0}}
    b = compute_grade(cfg, 40, 80, works_pct=50.0, ai_mark=None, quiz_pct=None)
    assert b.grade == 60
    # Full marks clamp to max_grade.
    top = compute_grade(cfg, 40, 80, works_pct=100.0, ai_mark=None, quiz_pct=None)
    assert top.grade == 80


def test_no_components_floors_to_min_grade() -> None:
    b = compute_grade(None, 10, 100, works_pct=None, ai_mark=None, quiz_pct=None)
    assert b.grade == 10


def test_defaults_are_code_only_works_only() -> None:
    # Empty config → code_weight 1, quiz_weight 0, works_weight 1, quality_weight 0.
    b = compute_grade({}, 0, 100, works_pct=75.0, ai_mark=99.0, quiz_pct=10.0)
    assert b.grade == 75  # quality and quiz weights default to 0
