"""Backfill: tick the check on accepted work, settle, and refuse to change any grade."""

from __future__ import annotations

from sqlalchemy import select

from submissions_checker.cli.backfill_requirements import backfill
from submissions_checker.db.models import StudentAssignment
from submissions_checker.db.models.enums import SubmissionStatus
from tests.functional._scored_helpers import CFG, arrange, attempt, squad_of_two

COMPLETED = SubmissionStatus.COMPLETED


async def test_backfill_ticks_completed_and_keeps_grades(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa.grade = 10
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    report = await backfill(db, asg.subject_id, apply=True)
    await db.commit()
    await db.refresh(sa)
    assert sa.teacher_scores == {"report": 4, "accepted": 1}
    assert report.ticked == 1 and report.settled == 1
    assert report.grade_changes == [] and sa.grade == 10


async def test_backfill_aborts_on_grade_change(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa.grade = 3  # inconsistent with the facts
    sa_id = sa.id
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    report = await backfill(db, asg.subject_id, apply=True)
    assert report.grade_changes == [(sa_id, 3, 10)]
    await db.refresh(sa)
    assert sa.grade == 3 and "accepted" not in (sa.teacher_scores or {})


async def test_dry_run_writes_nothing(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa.grade = 10
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    report = await backfill(db, asg.subject_id, apply=False)
    assert report.ticked == 1 and report.grade_changes == []
    await db.refresh(sa)
    assert sa.teacher_scores == {"report": 4}


async def test_squad_is_ticked_on_every_member(db, teacher, make_student):
    sa_a, sa_b, _ = await squad_of_two(
        db,
        teacher,
        make_student,
        config=CFG,
        scores={"report": 5},
        both_passed=True,
        status=COMPLETED,
    )
    for row in (sa_a, sa_b):
        row.grade = 13
    await db.commit()
    asg_id = sa_a.subjects_assignment_id
    report = await backfill(db, await _subject_id(db, asg_id), apply=True)
    await db.commit()
    assert report.ticked == 2 and report.grade_changes == []
    rows = (await db.execute(select(StudentAssignment).order_by(StudentAssignment.id))).scalars()
    for row in rows:
        await db.refresh(row)
        assert row.teacher_scores == {"report": 5, "accepted": 1}
        assert row.grade == 13


async def test_not_completed_is_not_ticked(db, teacher, make_student):
    _, asg, _, sa, _ = await arrange(db, teacher, make_student, config=CFG, scores={"report": 4})
    report = await backfill(db, asg.subject_id, apply=True)
    assert report.ticked == 0
    await db.refresh(sa)
    assert "accepted" not in (sa.teacher_scores or {})


async def _subject_id(db, asg_id: int) -> int:
    from submissions_checker.db.models import SubjectsAssignment

    asg = await db.get(SubjectsAssignment, asg_id)
    assert asg is not None
    return asg.subject_id
