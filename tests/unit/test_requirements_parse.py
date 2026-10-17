"""requirements.parse / validate: the new-style `requirements:` list."""

from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq

LAB4 = {
    "min_grade": 0,
    "max_grade": 16,
    "source": "classroom",
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5, "llm": "rubric text"},
        {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
        {"type": "check", "key": "accepted", "title": "Прийнято викладачем", "points": 0},
    ],
}


def test_parse_keeps_order_types_and_points() -> None:
    plan = rq.parse(LAB4)
    assert [(i.key, i.type, i.points, i.optional) for i in plan.items] == [
        ("quiz", "quiz", 8, False),
        ("report", "score", 5, False),
        ("star", "score", 3, True),
        ("accepted", "check", 0, False),
    ]
    assert plan.source == "classroom"
    assert plan.items[1].llm == "rubric text"
    assert [i.key for i in plan.teacher_items()] == ["report", "star", "accepted"]
    assert [i.key for i in plan.llm_items()] == ["report"]


def test_source_defaults_to_upload_when_code_items_present() -> None:
    cfg = {"max_grade": 10, "requirements": [{"type": "tests", "key": "t", "points": 10}]}
    assert rq.parse(cfg).source == "upload"


def test_source_defaults_to_none_without_code_items() -> None:
    cfg = {"max_grade": 2, "requirements": [{"type": "score", "key": "r", "title": "R", "max": 2}]}
    assert rq.parse(cfg).source is None


def test_validate_accepts_lab4() -> None:
    rq.validate("lab4", LAB4, has_quiz_block=True)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"max_grade": 17}, "must equal max_grade - min_grade"),
        ({"review_mode": "quiz_and_teacher_scores"}, "review_mode"),
        ({"source": "ftp"}, "source"),
    ],
)
def test_validate_rejects_bad_assignment(patch: dict, message: str) -> None:
    with pytest.raises(rq.RequirementsError, match=message):
        rq.validate("lab4", {**LAB4, **patch}, has_quiz_block=True)


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"type": "check", "key": "a", "title": "A", "max": 1}, "check 'a' takes points"),
        ({"type": "check", "key": "a", "title": "A", "points": 0, "llm": "x"}, "llm"),
        ({"type": "score", "key": "a", "max": 1}, "title"),
        ({"type": "score", "key": "A b", "title": "A", "max": 1}, "key"),
        ({"type": "magic", "key": "a", "points": 0}, "type"),
        ({"type": "score", "key": "report", "title": "dup", "max": 0}, "duplicate"),
    ],
)
def test_validate_rejects_bad_item(item: dict, message: str) -> None:
    cfg = {**LAB4, "requirements": [*LAB4["requirements"], item]}
    with pytest.raises(rq.RequirementsError, match=message):
        rq.validate("lab4", cfg, has_quiz_block=True)


def test_quiz_item_needs_quiz_block() -> None:
    with pytest.raises(rq.RequirementsError, match="quiz block"):
        rq.validate("lab4", LAB4, has_quiz_block=False)


def test_quiz_block_needs_quiz_item() -> None:
    cfg = {**LAB4, "max_grade": 8, "requirements": LAB4["requirements"][1:]}
    with pytest.raises(rq.RequirementsError, match="no quiz item"):
        rq.validate("lab4", cfg, has_quiz_block=True)


def test_code_items_refused_with_classroom_source() -> None:
    cfg = {
        **LAB4,
        "max_grade": 18,
        "requirements": [*LAB4["requirements"], {"type": "tests", "key": "t", "points": 2}],
    }
    with pytest.raises(rq.RequirementsError, match="not supported yet"):
        rq.validate("lab4", cfg, has_quiz_block=True)
