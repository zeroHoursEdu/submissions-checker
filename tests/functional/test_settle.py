"""settle(): the one place a student's assignment gets its grade and status."""

from __future__ import annotations

import asyncio

import pytest

from submissions_checker.db.models import StudentAssignment
from submissions_checker.db.models.enums import SubmissionStatus
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

    async def run(sa_id: int) -> SubmissionStatus | None:
        async with functional_sessionmaker() as s:
            row = await s.get(StudentAssignment, sa_id)
            res = await settle.settle(s, row)
            await s.commit()
            return res.new_status

    statuses = await asyncio.gather(run(sa_a.id), run(sa_b.id))
    assert statuses == [SubmissionStatus.COMPLETED, SubmissionStatus.COMPLETED]
    for sa_id in (sa_a.id, sa_b.id):
        async with functional_sessionmaker() as s:
            assert (await s.get(StudentAssignment, sa_id)).grade == 13  # ceil(8) + 5 + 0
