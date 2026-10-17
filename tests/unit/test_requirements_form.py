from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq

PLAN = rq.parse(
    {
        "max_grade": 8,
        "requirements": [
            {"type": "score", "key": "report", "title": "Звіт", "max": 5},
            {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
            {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
        ],
    }
)


def test_scores_and_check_parsed() -> None:
    form = {"score_report": " 4 ", "score_star": "", "score_accepted": "on"}
    assert rq.parse_scores_form(PLAN, form) == {"report": 4, "accepted": 1}


def test_custom_prefix() -> None:
    assert rq.parse_scores_form(PLAN, {"s7__report": "3"}, prefix="s7__") == {"report": 3}


@pytest.mark.parametrize("raw", ["6", "-1", "4.5", "²", "abc"])
def test_bad_score_rejected(raw: str) -> None:
    with pytest.raises(rq.ScoreError, match="Звіт"):
        rq.parse_scores_form(PLAN, {"score_report": raw})


def test_saved_check_cannot_be_cleared() -> None:
    assert rq.parse_scores_form(PLAN, {"score_report": "2"}, locked={"accepted": 1}) == {
        "report": 2,
        "accepted": 1,
    }


def test_row_signature_is_order_independent() -> None:
    a = rq.row_signature({"report": "4", "accepted": ""})
    assert a == rq.row_signature({"accepted": "", "report": "4"})
    assert a != rq.row_signature({"report": "5", "accepted": ""})
    assert len(a) == 16


def test_board_values_render_scores_and_checks() -> None:
    from submissions_checker.services.board_scores import board_values

    assert board_values(PLAN, {"report": 4, "accepted": 1}) == {
        "report": "4",
        "star": "",
        "accepted": "1",
    }
    assert board_values(PLAN, {}) == {"report": "", "star": "", "accepted": ""}


def test_posted_values_treat_saved_check_as_rendered() -> None:
    from submissions_checker.services.board_scores import board_values, posted_values

    saved = {"report": 4, "accepted": 1}
    # A saved check renders no input: nothing is posted for it, yet it reads as "1".
    posted = posted_values(PLAN, {"report": " 4 "}, saved)
    assert posted == {"report": "4", "star": "", "accepted": "1"}
    assert rq.row_signature(posted) == rq.row_signature(board_values(PLAN, saved))


def test_posted_values_tick_reads_as_one() -> None:
    from submissions_checker.services.board_scores import posted_values

    assert posted_values(PLAN, {"accepted": "on"}, {})["accepted"] == "1"
    assert posted_values(PLAN, {}, {})["accepted"] == ""
