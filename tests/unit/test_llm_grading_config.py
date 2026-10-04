import copy

import pytest

from submissions_checker.services.llm_grading.config import (
    inline_task,
    is_llm_graded,
    llm_criteria,
    subject_uses_llm,
    validate,
)

BASE = {
    "review_mode": "quiz_and_teacher_scores",
    "quiz": {"questions": [{}]},
    "grading": {
        "quiz_points": 4,
        "teacher_criteria": [
            {"key": "report", "title": "Звіт", "max": 3, "requirements": "Є висновки"},
            {"key": "oral", "title": "Усно", "max": 3, "llm": False},
        ],
    },
    "llm_grading": {"enabled": True, "source": "google_classroom", "task": "Зробіть лабу"},
}


def _cfg(**over):
    c = copy.deepcopy(BASE)
    c["llm_grading"].update(over)
    return c


def no_files(_):
    return None


def test_valid_inline_task():
    validate("l1", _cfg(), no_files)


def test_rejects_other_mode():
    c = _cfg()
    c["review_mode"] = "tests_only"
    with pytest.raises(ValueError, match="quiz_and_teacher_scores"):
        validate("l1", c, no_files)


def test_rejects_bad_source():
    with pytest.raises(ValueError, match="source"):
        validate("l1", _cfg(source="drive"), no_files)


def test_rejects_non_bool_enabled():
    with pytest.raises(ValueError, match="enabled"):
        validate("l1", _cfg(enabled="yes"), no_files)


def test_rejects_missing_task():
    c = _cfg()
    del c["llm_grading"]["task"]
    with pytest.raises(ValueError, match="task"):
        validate("l1", c, no_files)


def test_rejects_missing_task_file():
    c = _cfg(task_file="tasks/l1.md")
    del c["llm_grading"]["task"]
    with pytest.raises(ValueError, match="tasks/l1.md"):
        validate("l1", c, no_files)


def test_task_file_inlined():
    c = _cfg(task_file="tasks/l1.md")
    del c["llm_grading"]["task"]
    validate("l1", c, lambda p: "Текст" if p == "tasks/l1.md" else None)
    inline_task(c, lambda p: "Текст")
    assert c["llm_grading"]["task"] == "Текст"


def test_rejects_llm_criterion_without_requirements():
    c = _cfg()
    c["grading"]["teacher_criteria"][0]["requirements"] = "  "
    with pytest.raises(ValueError, match="report"):
        validate("l1", c, no_files)


def test_rejects_no_llm_criterion():
    c = _cfg()
    c["grading"]["teacher_criteria"][0]["llm"] = False
    with pytest.raises(ValueError, match="at least one"):
        validate("l1", c, no_files)


def test_helpers():
    assert is_llm_graded(_cfg()) and not is_llm_graded(
        {**_cfg(), "llm_grading": {"enabled": False}}
    )
    assert [c.key for c in llm_criteria(BASE["grading"])] == ["report"]
    assert subject_uses_llm([None, {}, _cfg()]) and not subject_uses_llm([{}])
