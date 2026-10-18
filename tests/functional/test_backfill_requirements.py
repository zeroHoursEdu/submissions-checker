"""Backfill: tick the check on accepted work, settle, and refuse to change any grade."""

from __future__ import annotations

from sqlalchemy import select

from submissions_checker.cli import backfill_requirements as bf
from submissions_checker.cli.backfill_requirements import backfill
from submissions_checker.db.models import StudentAssignment, SubjectsStudents
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.db.models.squad import SquadMember
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


async def test_second_apply_run_changes_nothing(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa.grade = 10
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    first = await backfill(db, asg.subject_id, apply=True)
    await db.commit()
    second = await backfill(db, asg.subject_id, apply=True)
    await db.commit()
    assert first.ticked == 1
    assert second.ticked == 0 and second.grade_changes == [] and second.status_changes == []
    await db.refresh(sa)
    assert sa.teacher_scores == {"report": 4, "accepted": 1} and sa.grade == 10


async def test_none_grade_becoming_a_value_is_a_grade_change(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa_id = sa.id
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    report = await backfill(db, asg.subject_id, apply=True)
    assert report.grade_changes == [(sa_id, None, 10)]
    await db.refresh(sa)
    assert sa.grade is None and "accepted" not in (sa.teacher_scores or {})


async def test_value_becoming_none_is_a_grade_change(db, teacher, make_student, monkeypatch):
    _, asg, _, sa, _ = await arrange(
        db, teacher, make_student, config=CFG, status=COMPLETED, scores={"report": 4}
    )
    sa.grade = 7
    await db.commit()
    sa_id = sa.id
    real = bf.settle_service.settle

    async def clearing(session, row, **kw):
        result = await real(session, row, **kw)
        row.grade = None
        await session.flush()
        return result

    monkeypatch.setattr(bf.settle_service, "settle", clearing)
    report = await backfill(db, asg.subject_id, apply=True)
    assert (sa_id, 7, None) in report.grade_changes
    await db.refresh(sa)
    assert sa.grade == 7


async def test_unenrolled_squad_mate_does_not_break_the_run(db, teacher, make_student):
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
    squad_id = await db.scalar(select(SquadMember.squad_id).limit(1))
    asg_id = sa_a.subjects_assignment_id
    subject_id = await _subject_id(db, asg_id)
    mate = await make_student(full_name="Ghost G")  # in the squad, no SA row, not enrolled
    db.add(SquadMember(squad_id=squad_id, student_id=mate.id, subject_id=subject_id))
    await db.commit()
    assert (
        await db.scalar(
            select(SubjectsStudents.student_id).where(SubjectsStudents.student_id == mate.id)
        )
        is None
    )
    report = await backfill(db, subject_id, apply=False)
    assert report.grade_changes == [] and report.settled >= 2


async def _subject_id(db, asg_id: int) -> int:
    from submissions_checker.db.models import SubjectsAssignment

    asg = await db.get(SubjectsAssignment, asg_id)
    assert asg is not None
    return asg.subject_id
