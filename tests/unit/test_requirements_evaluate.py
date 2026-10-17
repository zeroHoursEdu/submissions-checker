"""requirements.evaluate: item states, completion and the grade."""

from __future__ import annotations

from submissions_checker.services import requirements as rq

PLAN = rq.parse(
    {
        "max_grade": 16,
        "requirements": [
            {"type": "quiz", "key": "quiz", "points": 8},
            {"type": "score", "key": "report", "title": "Звіт", "max": 5},
            {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
            {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
        ],
    }
)


def ev(facts: rq.Facts, *, round_up: bool = False) -> rq.Evaluation:
    return rq.evaluate(PLAN, facts, 0, 16, round_up=round_up)


def test_nothing_done_is_pending_without_grade() -> None:
    e = ev(rq.Facts())
    assert [i.state for i in e.items] == ["pending"] * 4
    assert (e.complete, e.failed, e.grade, e.breakdown) == (False, False, None, None)
    assert e.pending_teacher() is True


def test_quiz_and_report_without_check_is_not_complete() -> None:
    e = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4}))
    assert e.complete is False and e.grade is None
    assert e.first_pending().key == "accepted"


def test_all_required_done_gives_grade_and_optional_adds() -> None:
    e = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4, "accepted": 1}))
    assert e.complete and e.grade == 10  # 6 + 4 + 0
    e2 = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4, "star": 2, "accepted": 1}))
    assert e2.grade == 12
    assert [i["key"] for i in e2.breakdown["items"]] == ["quiz", "report", "star", "accepted"]
    assert e2.breakdown["mode"] == "requirements" and e2.breakdown["quiz_score"] == 75.0


def test_half_up_and_squad_ceil() -> None:
    facts = rq.Facts(quiz=("done", 56.25), scores={"report": 0, "accepted": 1})  # 4.5 pts
    assert ev(facts).grade == 5
    facts = rq.Facts(quiz=("done", 53.0), scores={"report": 0, "accepted": 1})  # 4.24 pts
    assert ev(facts).grade == 4
    assert ev(facts, round_up=True).grade == 5


def test_failed_required_item_fails_the_work() -> None:
    e = ev(rq.Facts(quiz=("failed", None), scores={"report": 5, "accepted": 1}))
    assert e.failed and not e.complete and e.grade is None


def test_failed_optional_item_does_not_fail() -> None:
    plan = rq.parse(
        {
            "max_grade": 3,
            "requirements": [
                {"type": "tests", "key": "t", "points": 2, "optional": True},
                {"type": "check", "key": "a", "title": "A", "points": 1},
            ],
        }
    )
    e = rq.evaluate(plan, rq.Facts(tests=("failed", None), scores={"a": 1}), 0, 3, round_up=False)
    assert e.complete and not e.failed and e.grade == 1


def test_tests_and_ai_points_scale_by_percent() -> None:
    plan = rq.parse(
        {
            "max_grade": 12,
            "requirements": [
                {"type": "tests", "key": "t", "points": 10},
                {"type": "ai_review", "key": "ai", "points": 2},
            ],
        }
    )
    e = rq.evaluate(
        plan, rq.Facts(tests=("done", 80.0), ai_review=("done", 50.0)), 0, 12, round_up=False
    )
    assert (
        e.grade == 9 and e.breakdown["works_score"] == 80.0 and e.breakdown["quality_score"] == 50.0
    )


def test_running_item_blocks_completion() -> None:
    plan = rq.parse(
        {"max_grade": 10, "requirements": [{"type": "tests", "key": "t", "points": 10}]}
    )
    e = rq.evaluate(plan, rq.Facts(tests=("running", None)), 0, 10, round_up=False)
    assert not e.complete and not e.failed and e.items[0].state == "running"


def test_grade_is_clamped_to_range() -> None:
    plan = rq.parse(
        {
            "min_grade": 1,
            "max_grade": 2,
            "requirements": [{"type": "score", "key": "s", "title": "S", "max": 1}],
        }
    )
    assert rq.evaluate(plan, rq.Facts(scores={"s": 1}), 1, 2, round_up=True).grade == 2
