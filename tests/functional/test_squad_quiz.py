"""The pair quiz: disjoint slices, per-member pass, grade only when both passed."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from submissions_checker.db.models import QuizAttempt, StudentAssignment, Submission
from submissions_checker.db.models.enums import SubmissionStatus
from tests.functional.test_squad_submission import _arrange, _client, _lock_pair, _zip

pytestmark = pytest.mark.asyncio


async def _upload(client, subject, sa) -> None:
    r = await client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("r.zip", _zip(), "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 303


async def _start(client, subject, sa) -> int:
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


async def _answer_all(client, db, attempt_id: int, *, correct: bool) -> None:
    attempt = await db.get(QuizAttempt, attempt_id)
    form = {f"answer_{q['id']}": "1" if correct else "0" for q in attempt.questions_snapshot}
    r = await client.post(f"/portal/quiz/{attempt_id}/submit", data=form, follow_redirects=False)
    assert r.status_code == 303


async def test_pair_gets_disjoint_halves_and_a_unified_grade(
    db, make_user, make_student, teacher, teacher_client
) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_a = await _start(ca, subject, sa_a)
        att_b = await _start(cb, subject, sa_b)
        a = await db.get(QuizAttempt, att_a)
        b = await db.get(QuizAttempt, att_b)
        ids_a = {q["id"] for q in a.questions_snapshot}
        ids_b = {q["id"] for q in b.questions_snapshot}
        assert ids_a.isdisjoint(ids_b) and ids_a | ids_b == {0, 1, 2, 3}
        assert a.student_id == ua.student_id and b.student_id == ub.student_id
        assert a.config_snapshot["squad"]["member_count"] == 2

        await _answer_all(ca, db, att_a, correct=True)
        sub = (await db.execute(select(Submission))).scalar_one()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.QUIZ_SENT  # B still owes their half
        assert (await db.get(StudentAssignment, sa_a.id)).grade is None
        page_a = await ca.get(f"/portal/subjects/{subject.id}/assignments/{sa_a.id}")
        # Jinja autoescapes the apostrophe in the vocab string to &#39; in the raw HTML.
        assert "Оцінка з" in page_a.text and "явиться" in page_a.text

        await _answer_all(cb, db, att_b, correct=True)
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW

    r = await teacher_client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "approve", "reason": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(sub)
    ga = await db.get(StudentAssignment, sa_a.id)
    gb = await db.get(StudentAssignment, sa_b.id)
    await db.refresh(ga)
    await db.refresh(gb)
    assert ga.grade == gb.grade == 8
    assert sub.grade_breakdown["squad"]["unified"] is True
    assert len(sub.grade_breakdown["squad"]["members"]) == 2


async def test_failing_member_retries_only_their_half(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_a = await _start(ca, subject, sa_a)
        await _answer_all(ca, db, att_a, correct=True)
        att_b1 = await _start(cb, subject, sa_b)
        await _answer_all(cb, db, att_b1, correct=False)
        att_b2 = await _start(cb, subject, sa_b)
        assert att_b2 != att_b1
        b2 = await db.get(QuizAttempt, att_b2)
        assert len(b2.questions_snapshot) == 2
        sub = (await db.execute(select(Submission))).scalar_one()
        assert sub.status == SubmissionStatus.QUIZ_SENT
        # A's own quiz link must not start a new attempt: they already passed.
        r = await ca.get(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/quiz", follow_redirects=False
        )
        assert r.headers["location"].endswith(f"/portal/quiz/{att_a}/result")


async def test_exhausted_member_fails_the_squad(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        for _ in range(2):
            att = await _start(cb, subject, sa_b)
            await _answer_all(cb, db, att, correct=False)
        sub = (await db.execute(select(Submission))).scalar_one()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.FAILED
        page_a = await ca.get(f"/portal/subjects/{subject.id}/assignments/{sa_a.id}")
        assert "FAILED" in page_a.text or "Не зараховано" in page_a.text


async def test_member_cannot_open_partners_attempt(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_a = await _start(ca, subject, sa_a)
        r = await cb.get(f"/portal/quiz/{att_a}")
        assert r.status_code == 403


_QUIZ_REQUIRED_HEAVY = {
    "questions": [
        {
            "type": "single_choice",
            "text": f"q{i}",
            "points": 1,
            "options": ["w", "r"],
            "correct": 1,
            "required": True,
        }
        for i in range(3)
    ]
    + [
        {
            "type": "single_choice",
            "text": "q3",
            "points": 1,
            "options": ["w", "r"],
            "correct": 1,
            "required": False,
        }
    ],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.5,
    "max_quiz_attempts": 2,
    "questions_to_send": 4,
}


async def test_retry_keeps_slice_size_when_required_outnumber_slice(
    db, make_user, make_student, teacher
) -> None:
    """3 required + 1 optional, squad of 2 -> a 2-question slice. A retry must stay at 2
    questions even when both of a member's original questions were required — the config
    still has 3 required questions overall, more than that member's slice ever held."""
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(
        db, make_user, make_student, teacher, quiz=_QUIZ_REQUIRED_HEAVY
    )
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_b1 = await _start(cb, subject, sa_b)
        first = await db.get(QuizAttempt, att_b1)
        await _answer_all(cb, db, att_b1, correct=False)
        att_b2 = await _start(cb, subject, sa_b)
        assert att_b2 != att_b1
        second = await db.get(QuizAttempt, att_b2)
        assert len(second.questions_snapshot) == len(first.questions_snapshot)
        first_ids = {q["id"] for q in first.questions_snapshot}
        for q in second.questions_snapshot:
            if q["is_required"]:
                assert q["id"] in first_ids


async def test_quiz_pages_say_which_part_is_mine(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca:
        await _upload(ca, subject, sa_a)
        att = await _start(ca, subject, sa_a)
        page = await ca.get(f"/portal/quiz/{att}")
        assert "Ваша частина: 2 з 4 питань" in page.text and "Bohdan B" in page.text
        await _answer_all(ca, db, att, correct=True)
        result = await ca.get(f"/portal/quiz/{att}/result")
        # Jinja autoescapes the apostrophe in the vocab string to &#39; in the raw HTML.
        assert "Оцінка з" in result.text and "явиться" in result.text
