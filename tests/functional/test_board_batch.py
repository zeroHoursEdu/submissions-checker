"""Board batch save: changed rows only, partial failure, locked acceptance."""

from __future__ import annotations

import pytest

from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services import requirements as rq
from submissions_checker.services.board_scores import board_values
from tests.functional._scored_helpers import CFG, arrange, arrange_second_student, attempt

pytestmark = pytest.mark.asyncio


def _orig(scores):
    plan = rq.parse({"max_grade": 13, **CFG})
    return rq.row_signature(board_values(plan, scores))


def _batch(subject, asg) -> str:
    return f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores/batch"


async def test_batch_saves_changed_rows_only(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    s2, sa2 = await arrange_second_student(db, subject, asg, make_student, scores={"report": 3})
    form = {
        f"orig__{s1.id}": _orig({}),
        f"s{s1.id}__report": "4",
        f"s{s1.id}__accepted": "on",
        f"orig__{s2.id}": _orig({"report": 3}),
        f"s{s2.id}__report": "3",
    }
    r = await teacher_client.post(_batch(subject, asg), data=form, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    assert "skipped" not in r.headers["location"]
    await db.refresh(sa1)
    assert sa1.teacher_scores == {"report": 4, "accepted": 1}
    await db.refresh(sa2)
    assert sa2.teacher_scores == {"report": 3}


async def test_bad_row_does_not_roll_back_others(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    s2, sa2 = await arrange_second_student(db, subject, asg, make_student)
    form = {
        f"orig__{s1.id}": _orig({}),
        f"s{s1.id}__report": "9",  # above max
        f"orig__{s2.id}": _orig({}),
        f"s{s2.id}__report": "2",
    }
    r = await teacher_client.post(_batch(subject, asg), data=form, follow_redirects=False)
    loc = r.headers["location"]
    assert "saved=1" in loc and f"{s1.id}:invalid" in loc
    await db.refresh(sa2)
    assert sa2.teacher_scores == {"report": 2}
    await db.refresh(sa1)
    assert sa1.teacher_scores is None


async def test_batch_flash_names_skipped_rows(db, teacher_client, teacher, make_student):
    subject, asg, s1, _sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    page = await teacher_client.get(
        f"/teacher/subjects/{subject.id}/assignments/{asg.id}?saved=2&skipped={s1.id}:invalid"
    )
    assert "Збережено" in page.text and "Пропущено" in page.text
    assert "некоректний бал" in page.text and s1.full_name in page.text


async def test_stale_tab_cannot_clear_saved_check(db, teacher_client, teacher, make_student):
    """Review Focus 2."""
    subject, asg, s1, sa1, _ = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    form = {f"orig__{s1.id}": _orig({"report": 4}), f"s{s1.id}__report": "5"}  # no accepted
    await teacher_client.post(_batch(subject, asg), data=form, follow_redirects=False)
    await db.refresh(sa1)
    assert sa1.teacher_scores == {"report": 5, "accepted": 1}


async def test_untouched_row_with_saved_check_is_not_resaved(
    db, teacher_client, teacher, make_student
):
    subject, asg, s1, sa1, _ = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    # The saved check renders no input, so nothing is posted for it.
    form = {f"orig__{s1.id}": _orig({"report": 4, "accepted": 1}), f"s{s1.id}__report": "4"}
    r = await teacher_client.post(_batch(subject, asg), data=form, follow_redirects=False)
    assert "saved=0" in r.headers["location"]


async def test_saved_check_renders_without_input(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(
        db, teacher, make_student, config=CFG, scores={"accepted": 1}
    )
    html = (await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")).text
    assert f'name="s{s1.id}__accepted"' not in html
    assert "data-check-done" in html


async def test_unsaved_check_renders_checkbox_and_columns(
    db, teacher_client, teacher, make_student
):
    subject, asg, s1, _sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    html = (await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")).text
    assert f'name="s{s1.id}__accepted"' in html
    assert f'name="s{s1.id}__report"' in html
    assert f'name="orig__{s1.id}"' in html and f'value="{_orig({})}"' in html
    assert 'id="batch-form"' in html
    assert "Зберегти зміни" in html
    # The AI-draft copy is offered on LLM-graded boards only (see test_classroom_board).
    assert "Взяти бали з AI-чернетки" not in html
    assert "Звіт" in html and "Прийнято" in html and "/5" in html and "/8" in html


async def test_check_and_quiz_complete_the_work(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, s1.id, score=4, max_score=4, passed=True)
    form = {f"orig__{s1.id}": _orig({}), f"s{s1.id}__report": "5", f"s{s1.id}__accepted": "on"}
    await teacher_client.post(_batch(subject, asg), data=form, follow_redirects=False)
    await db.refresh(sa1)
    assert sa1.grade == 13
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED


async def test_approve_is_gone(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    r = await teacher_client.post(
        f"/teacher/submissions/{sub.id}/review", data={"action": "approve"}, follow_redirects=False
    )
    assert r.status_code == 400
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/assignments/{asg.id}/bulk",
        data={"action": "approve", "submission_ids": [sub.id]},
        follow_redirects=False,
    )
    assert r.status_code == 400
    html = (await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")).text
    assert 'value="approve"' not in html


async def test_reject_fails_with_reason(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    r = await teacher_client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "reject", "reason": "копія"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED
    assert sub.source_metadata["rejected"]["reason"] == "копія"
    assert sub.test_results["check_reason"] == "копія"


async def test_bulk_reject_works_on_quiz_sent(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(db, teacher, make_student, config=CFG)
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/assignments/{asg.id}/bulk",
        data={"action": "reject", "submission_ids": [sub.id], "reason": "копія"},
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("?bulk=1,0")
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED
