"""from_config: legacy review_mode configs become plans that grade exactly as before."""

from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq

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


# Expected grades captured once from the retired weighted-blend and scored-mode formulas.
_G = {
    "code_weight": 0.6,
    "quiz_weight": 0.4,
    "code": {"works_weight": 0.7, "quality_weight": 0.3},
}
_WEIGHTED = [
    # mode, grading, works, ai, quiz, {(lo, hi): expected}
    ("tests_only", {}, 73.0, None, None, {(0, 100): 73, (1, 5): 4, (60, 100): 89}),
    ("tests_then_ai", _G, 55.0, 81.0, None, {(0, 100): 63, (1, 5): 4, (60, 100): 85}),
    ("tests_then_quiz", _G, 100.0, None, 62.5, {(0, 100): 85, (1, 5): 4, (60, 100): 94}),
    ("tests_then_ai_then_quiz", _G, 40.0, 90.0, 75.0, {(0, 100): 63, (1, 5): 4, (60, 100): 85}),
    ("quiz_only", {"quiz_weight": 1.0}, None, None, 87.5, {(0, 100): 88, (1, 5): 4, (60, 100): 95}),
]
_WEIGHTED_CASES = [
    (mode, g, w, a, q, lo, hi, exp)
    for mode, g, w, a, q, table in _WEIGHTED
    for (lo, hi), exp in table.items()
]


@pytest.mark.parametrize(
    ("mode", "grading", "works", "ai", "quiz", "lo", "hi", "expected"), _WEIGHTED_CASES
)
def test_translated_weighted_grade_matches_frozen_oracle(
    mode: str,
    grading: dict,
    works: float | None,
    ai: float | None,
    quiz: float | None,
    lo: int,
    hi: int,
    expected: int,
) -> None:
    cfg = {"review_mode": mode, "grading": grading}
    facts = rq.Facts(
        quiz=("done", quiz) if quiz is not None else ("pending", None),
        tests=("done", works) if works is not None else ("pending", None),
        ai_review=("done", ai) if ai is not None else ("pending", None),
        checks_done=frozenset({"accepted"}),
    )
    got = rq.evaluate(rq.from_config(cfg, span=hi - lo), facts, lo, hi, round_up=False).grade
    assert got == expected


@pytest.mark.parametrize(
    ("quiz_pct", "expected"),
    [(0.0, 6), (31.25, 9), (56.25, 11), (68.75, 12), (100.0, 14)],
)
@pytest.mark.parametrize("round_up", [False, True])
def test_translated_scored_grade_matches_frozen_oracle(
    quiz_pct: float, expected: int, round_up: bool
) -> None:
    facts = rq.Facts(quiz=("done", quiz_pct), scores={"report": 4, "star": 2})
    assert rq.evaluate(rq.from_config(SCORED), facts, 0, 16, round_up=round_up).grade == expected
