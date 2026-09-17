"""Squad-mate rows on the board and grid show the shared submission; review page names members."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from submissions_checker.db.models import Submission
from tests.functional.test_squad_quiz import _answer_all, _start, _upload
from tests.functional.test_squad_submission import _arrange, _client, _lock_pair

pytestmark = pytest.mark.asyncio


async def test_board_and_grid_show_shared_submission_for_both_members(
    db, make_user, make_student, teacher, teacher_client
) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca:
        await _upload(ca, subject, sa_a)
        att = await _start(ca, subject, sa_a)
        await _answer_all(ca, db, att, correct=True)

    board = await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")
    assert board.text.count("👥") >= 2  # both rows carry the badge
    assert board.text.count("QUIZ_SENT") >= 2 or board.text.count("Тест") >= 2

    page = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "waiting_partner" in page.text or "чекаємо" in page.text


async def test_review_page_names_the_member_behind_each_attempt(
    db, make_user, make_student, teacher, teacher_client
) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        for client, sa in ((ca, sa_a), (cb, sa_b)):
            att = await _start(client, subject, sa)
            await _answer_all(client, db, att, correct=True)
    sub = (await db.execute(select(Submission))).scalar_one()
    page = await teacher_client.get(f"/teacher/submissions/{sub.id}/review")
    assert page.status_code == 200
    assert "Anna A" in page.text and "Bohdan B" in page.text
