"""from_config: legacy review_mode configs become plans that grade exactly as before."""

from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq
from submissions_checker.services import teacher_scores
from submissions_checker.services.grading import compute_grade

SCORED = {
    "review_mode": "quiz_and_teacher_scores",
    "llm_grading": {"enabled": True, "source": "google_classroom", "task": "t"},
    "grading": {
        "quiz_points": 8,
        "teacher_criteria": [
            {"key": "report", "title": "Звіт", "max": 5, "requirements": "rubric"},
            {"key": "star", "title": "Зірочка", "max": 3, "optional": True, "llm": False},
        ],
    },
}


def test_new_style_config_is_parsed_not_translated() -> None:
    cfg = {
        "max_grade": 1,
        "requirements": [{"type": "check", "key": "a", "title": "A", "points": 1}],
    }
    assert rq.from_config(cfg) == rq.parse(cfg)


def test_scored_mode_translation() -> None:
    plan = rq.from_config(SCORED)
    assert [(i.key, i.type, i.points, i.optional, i.llm) for i in plan.items] == [
        ("quiz", "quiz", 8, False, None),
        ("report", "score", 5, False, "rubric"),
        ("star", "score", 3, True, None),
    ]
    assert plan.source == "classroom"
    assert plan.rounding == "half_up"


@pytest.mark.parametrize(
    ("mode", "types"),
    [
        ("tests_only", ["tests"]),
        ("tests_then_ai", ["tests", "ai_review"]),
        ("tests_then_teacher", ["tests", "check"]),
        ("tests_then_ai_then_teacher", ["tests", "ai_review", "check"]),
        ("tests_then_ai_teacher", ["tests", "ai_review", "check"]),
        ("tests_then_quiz", ["tests", "quiz"]),
        ("tests_then_ai_then_quiz", ["tests", "ai_review", "quiz"]),
        ("quiz_only", ["quiz"]),
        ("quiz_then_teacher", ["quiz", "check"]),
        (None, ["tests"]),
    ],
)
def test_weighted_modes_translate_to_items(mode: str | None, types: list[str]) -> None:
    cfg = {} if mode is None else {"review_mode": mode}
    plan = rq.from_config(cfg)
    assert [i.type for i in plan.items] == types
    assert plan.source == "upload"
    assert plan.rounding == "bankers"


def _oracle_cases() -> list[tuple[str, dict, float | None, float | None, float | None]]:
    g = {
        "code_weight": 0.6,
        "quiz_weight": 0.4,
        "code": {"works_weight": 0.7, "quality_weight": 0.3},
    }
    return [
        ("tests_only", {}, 73.0, None, None),
        ("tests_then_ai", g, 55.0, 81.0, None),
        ("tests_then_quiz", g, 100.0, None, 62.5),
        ("tests_then_ai_then_quiz", g, 40.0, 90.0, 75.0),
        ("quiz_only", {"quiz_weight": 1.0}, None, None, 87.5),
    ]


@pytest.mark.parametrize(("mode", "grading", "works", "ai", "quiz"), _oracle_cases())
@pytest.mark.parametrize(("lo", "hi"), [(0, 100), (1, 5), (60, 100)])
def test_translated_weighted_grade_equals_old_compute_grade(
    mode: str,
    grading: dict,
    works: float | None,
    ai: float | None,
    quiz: float | None,
    lo: int,
    hi: int,
) -> None:
    cfg = {"review_mode": mode, "grading": grading}
    old = compute_grade(grading, lo, hi, works_pct=works, ai_mark=ai, quiz_pct=quiz).grade
    facts = rq.Facts(
        quiz=("done", quiz) if quiz is not None else ("pending", None),
        tests=("done", works) if works is not None else ("pending", None),
        ai_review=("done", ai) if ai is not None else ("pending", None),
        checks_done=frozenset({"accepted"}),
    )
    assert (
        rq.evaluate(rq.from_config(cfg, span=hi - lo), facts, lo, hi, round_up=False).grade == old
    )


@pytest.mark.parametrize("quiz_pct", [0.0, 31.25, 56.25, 68.75, 100.0])
@pytest.mark.parametrize("round_up", [False, True])
def test_translated_scored_grade_equals_old_compute(quiz_pct: float, round_up: bool) -> None:
    scores = {"report": 4, "star": 2}
    old = teacher_scores.compute(
        SCORED["grading"], 0, 16, quiz_pct=quiz_pct, scores=scores, round_up=round_up
    )["grade"]
    facts = rq.Facts(quiz=("done", quiz_pct), scores=scores)
    assert rq.evaluate(rq.from_config(SCORED), facts, 0, 16, round_up=round_up).grade == old
