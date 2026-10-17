"""settle(): the one place a student's assignment gets its grade and status."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from submissions_checker.db.models import StudentAssignment, Submission
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.services import settle
from tests.functional._scored_helpers import CFG, arrange, attempt, squad_of_two, submissions_of

pytestmark = pytest.mark.asyncio


async def test_quiz_passed_waits_for_teacher(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert res.evaluation.first_pending().key == "report"
    await db.refresh(sa)
    assert sa.grade is None


async def test_scores_then_check_complete_in_any_order(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.QUIZ_SENT  # only the quiz left
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.completed_now
    await db.refresh(sa)
    assert sa.grade == 10  # 6 + 4 + 0
    assert sub.grade_breakdown["mode"] == "requirements"


async def test_exhausted_quiz_fails(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG, max_attempts=1)
    await attempt(db, sub, student.id, score=0, max_score=4, passed=False)
    assert (await settle.settle(db, sa)).new_status == SubmissionStatus.FAILED


async def test_rejected_stays_failed(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    sub.source_metadata = {settle.REJECTED_KEY: {"reason": "copy"}}
    await attempt(db, sub, student.id, score=4, max_score=4, passed=True)
    assert (await settle.settle(db, sa)).new_status == SubmissionStatus.FAILED


async def test_completed_never_leaves_completed(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(
        db,
        teacher,
        make_student,
        config=CFG,
        status=SubmissionStatus.COMPLETED,
        scores={"report": 4},
    )
    res = await settle.settle(db, sa)  # check missing: incomplete, but already graded
    assert res.new_status == SubmissionStatus.COMPLETED


async def test_teacher_only_plan_without_submission_creates_one(db, teacher, make_student) -> None:
    cfg = {"requirements": [{"type": "score", "key": "r", "title": "R", "max": 16}]}
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=cfg, with_submission=False, scores={"r": 11}
    )
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 11


async def test_incomplete_without_submission_writes_nothing(db, teacher, make_student) -> None:
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=CFG, with_submission=False, scores={"report": 4}
    )
    res = await settle.settle(db, sa)
    assert res.submission_id is None and res.new_status is None
    assert await submissions_of(db, sa) == []


async def test_squad_concurrent_settle_one_transition(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    sa_a, sa_b, sub = await squad_of_two(
        db,
        teacher,
        make_student,
        config=CFG,
        scores={"report": 5, "accepted": 1},
        both_passed=True,
    )

    async def run(sa_id: int) -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            row = await s.get(StudentAssignment, sa_id)
            res = await settle.settle(s, row)
            await s.commit()
            return res

    results = await asyncio.gather(run(sa_a.id), run(sa_b.id))
    assert [r.new_status for r in results] == [SubmissionStatus.COMPLETED] * 2
    assert sum(r.completed_now for r in results) == 1
    for sa_id in (sa_a.id, sa_b.id):
        async with functional_sessionmaker() as s:
            assert (await s.get(StudentAssignment, sa_id)).grade == 13  # ceil(8) + 5 + 0


async def test_squad_last_attempts_racing_still_complete(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    """A and B each add their own passing attempt and settle in their own transaction.
    A settles first and commits a moment later; B settles while A is uncommitted. B must
    wait for A and only then read the facts (seeing A's pass), or nobody completes."""
    sa_a, sa_b, sub = await squad_of_two(
        db, teacher, make_student, config=CFG, scores={"report": 5, "accepted": 1}
    )
    # Both attempts are inserted before either settles (an attempt insert takes a key-share
    # lock on the submission, so inserting after A locked it would serialise by accident).
    b_inserted = asyncio.Event()
    a_settled = asyncio.Event()

    async def member_a() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            await attempt(s, sub, sa_a.student_id, score=1, max_score=1, passed=True, commit=False)
            await b_inserted.wait()
            res = await settle.settle(s, await s.get(StudentAssignment, sa_a.id))
            a_settled.set()
            await asyncio.sleep(0.5)  # B is inside settle now, waiting on A's lock
            await s.commit()
            return res

    async def member_b() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            await attempt(s, sub, sa_b.student_id, score=1, max_score=1, passed=True, commit=False)
            b_inserted.set()
            await a_settled.wait()
            res = await settle.settle(s, await s.get(StudentAssignment, sa_b.id))
            await s.commit()
            return res

    res_a, res_b = await asyncio.wait_for(asyncio.gather(member_a(), member_b()), timeout=15)
    assert res_a.new_status == SubmissionStatus.QUIZ_SENT  # B's pass not committed yet
    assert res_b.completed_now
    async with functional_sessionmaker() as s:
        assert (await s.get(Submission, sub.id)).status == SubmissionStatus.COMPLETED
        for sa_id in (sa_a.id, sa_b.id):
            assert (await s.get(StudentAssignment, sa_id)).grade == 13


async def test_concurrent_settles_create_one_upload_less_submission(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    cfg = {"requirements": [{"type": "score", "key": "r", "title": "R", "max": 16}]}
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=cfg, with_submission=False, scores={"r": 11}
    )

    async def run() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            res = await settle.settle(s, await s.get(StudentAssignment, sa.id))
            await s.commit()
            return res

    results = await asyncio.gather(run(), run())
    assert sum(r.completed_now for r in results) == 1
    async with functional_sessionmaker() as s:
        n = await s.scalar(
            select(func.count(Submission.id)).where(Submission.students_assignment_id == sa.id)
        )
        assert n == 1


async def test_previous_status_drives_the_teacher_notification(db, teacher, make_student) -> None:
    """A worker applies processing_done (-> AWAITING_TEACHER_REVIEW) and then settles; the
    teacher must still hear about it although the status at lock time is already ATR."""
    _, _, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    await attempt(db, sub, student.id, score=4, max_score=4, passed=True)

    async def queued() -> int:
        return await db.scalar(
            select(func.count())
            .select_from(TeacherNotificationQueue)
            .where(TeacherNotificationQueue.submission_id == sub.id)
        )

    await settle.settle(db, sa)
    assert await queued() == 0
    res = await settle.settle(db, sa, previous_status=SubmissionStatus.TESTING)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert await queued() == 1
