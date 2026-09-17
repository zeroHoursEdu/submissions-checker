"""Rules of services.squads against a real Postgres (db_session fixture)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from submissions_checker.db.models import (
    Group,
    Student,
    Subject,
    SubjectsStudents,
    SubmissionSourceType,
    User,
    UserRole,
)
from submissions_checker.db.models.enums import EntityType, SquadInviteStatus
from submissions_checker.db.models.squad import SquadInvite
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.services import squads
from submissions_checker.services.squads import SquadError

pytestmark = pytest.mark.asyncio


async def _subject(db, *, max_size: int | None = 2, students: int = 3):
    group = Group(name=f"G-{datetime.now(UTC).timestamp()}")
    subject = Subject(name="S", squad_max_size=max_size)
    db.add_all([group, subject])
    await db.flush()
    asg = SubjectsAssignment(subject_id=subject.id, title="L1", code="l1", config={})
    db.add(asg)
    await db.flush()
    people = []
    for i in range(students):
        s = Student(group_id=group.id, email=f"s{i}-{subject.id}@x", full_name=f"S{i}")
        db.add(s)
        await db.flush()
        db.add(SubjectsStudents(subject_id=subject.id, student_id=s.id))
        db.add(StudentAssignment(student_id=s.id, subjects_assignment_id=asg.id))
        u = User(
            username=f"u{i}-{subject.id}", password_hash="x", role=UserRole.STUDENT, student_id=s.id
        )
        db.add(u)
        people.append(s)
    await db.flush()
    return subject, asg, people


async def _submit(
    db, student: Student, asg: SubjectsAssignment, squad_id: int | None = None
) -> Submission:
    sa_id = (
        (
            await db.execute(
                StudentAssignment.__table__.select().where(
                    StudentAssignment.student_id == student.id,
                    StudentAssignment.subjects_assignment_id == asg.id,
                )
            )
        )
        .one()
        .id
    )
    sub = Submission(
        students_assignment_id=sa_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        squad_id=squad_id,
    )
    db.add(sub)
    await db.flush()
    return sub


async def test_eligibility_matrix(db_session) -> None:
    subject, asg, (a, b, c) = await _subject(db_session)
    assert await squads.eligibility(db_session, subject.id, a.id) is None
    await _submit(db_session, b, asg)
    assert await squads.eligibility(db_session, subject.id, b.id) == "has_submissions"
    off, _, (z,) = await _subject(db_session, max_size=None, students=1)
    assert await squads.eligibility(db_session, off.id, z.id) == "disabled"
    assert await squads.eligibility(db_session, subject.id, z.id) == "not_enrolled"
    c.type = EntityType.TEST
    await db_session.flush()
    assert await squads.eligibility(db_session, subject.id, c.id) == "test_student"


async def test_invite_accept_locks_at_max_and_cancels_other_invites(db_session) -> None:
    subject, asg, (a, b, c) = await _subject(db_session)
    squad = await squads.create_with_invites(db_session, subject.id, a.id, [b.id])
    other = await squads.create_with_invites(db_session, subject.id, c.id, [b.id])
    assert squad.locked_at is None
    inv = (
        await db_session.execute(
            SquadInvite.__table__.select().where(SquadInvite.squad_id == squad.id)
        )
    ).one()
    locked = await squads.accept_invite(db_session, inv.id, b.id)
    assert locked.locked_at is not None
    assert {m.student_id for m in locked.members} == {a.id, b.id}
    assert {m.student.full_name for m in locked.members} == {"S0", "S1"}
    # B's other pending invite (C's) is cancelled by the join — and since that leaves
    # C's squad with one member and no pending invites, I4 deletes it outright, taking
    # the invite row with it via the FK cascade. Nothing left to select.
    other_invites = (
        await db_session.execute(
            SquadInvite.__table__.select().where(SquadInvite.squad_id == other.id)
        )
    ).all()
    assert other_invites == []
    assert await squads.squad_of(db_session, subject.id, c.id) is None
    assert await squads.eligibility(db_session, subject.id, a.id) == "already_in_squad"


async def test_accept_elsewhere_deletes_orphaned_single_member_squad(db_session) -> None:
    """I4: A and C both invite B. B accepts A's invite — the sweep that cancels B's
    other pending invite (C's) leaves C's squad at one member with no pending
    invites; it must be deleted rather than left dangling."""
    subject, asg, (a, b, c) = await _subject(db_session)
    await squads.create_with_invites(db_session, subject.id, a.id, [b.id])
    await squads.create_with_invites(db_session, subject.id, c.id, [b.id])
    inv_a_id = await db_session.scalar(
        select(SquadInvite.id).where(
            SquadInvite.invited_student_id == b.id, SquadInvite.invited_by_student_id == a.id
        )
    )
    await squads.accept_invite(db_session, inv_a_id, b.id)

    assert await squads.squad_of(db_session, subject.id, c.id) is None
    assert await squads.eligibility(db_session, subject.id, c.id) is None


async def test_accept_is_refused_after_partner_submitted(db_session) -> None:
    subject, asg, (a, b, _) = await _subject(db_session)
    squad = await squads.create_with_invites(db_session, subject.id, a.id, [b.id])
    inv = (
        await db_session.execute(
            SquadInvite.__table__.select().where(SquadInvite.squad_id == squad.id)
        )
    ).one()
    await _submit(db_session, b, asg)
    with pytest.raises(SquadError) as exc:
        await squads.accept_invite(db_session, inv.id, b.id)
    assert exc.value.reason == "has_submissions"
    refreshed = await db_session.get(SquadInvite, inv.id)
    assert refreshed.status == SquadInviteStatus.CANCELLED


async def test_cancel_last_invite_deletes_empty_squad(db_session) -> None:
    subject, asg, (a, b, _) = await _subject(db_session)
    squad = await squads.create_with_invites(db_session, subject.id, a.id, [b.id])
    inv = (
        await db_session.execute(
            SquadInvite.__table__.select().where(SquadInvite.squad_id == squad.id)
        )
    ).one()
    await squads.cancel_invite(db_session, inv.id, a.id)
    assert await squads.squad_of(db_session, subject.id, a.id) is None


async def test_create_rejects_too_many_and_self(db_session) -> None:
    subject, asg, (a, b, c) = await _subject(db_session)
    with pytest.raises(SquadError) as exc:
        await squads.create_with_invites(db_session, subject.id, a.id, [b.id, c.id])
    assert exc.value.reason == "too_many"
    with pytest.raises(SquadError) as exc2:
        await squads.create_with_invites(db_session, subject.id, a.id, [a.id])
    assert exc2.value.reason == "self_invite"


async def test_create_and_assign_reject_duplicate_ids(db_session) -> None:
    subject, asg, (a, b, _) = await _subject(db_session)
    with pytest.raises(SquadError) as exc:
        await squads.create_with_invites(db_session, subject.id, a.id, [b.id, b.id])
    assert exc.value.reason == "duplicate"
    teacher = User(username="t-dup", password_hash="x", role=UserRole.TEACHER)
    db_session.add(teacher)
    await db_session.flush()
    with pytest.raises(SquadError) as exc2:
        await squads.teacher_assign(db_session, subject.id, teacher.id, [b.id, b.id])
    assert exc2.value.reason == "duplicate"


async def test_teacher_assign_creates_locked_squad(db_session) -> None:
    subject, asg, (a, b, _) = await _subject(db_session)
    teacher = User(username="t", password_hash="x", role=UserRole.TEACHER)
    db_session.add(teacher)
    await db_session.flush()
    squad = await squads.teacher_assign(db_session, subject.id, teacher.id, [a.id, b.id], "Alpha")
    assert squad.locked_at is not None and squad.name == "Alpha"
    with pytest.raises(SquadError) as exc:
        await squads.teacher_assign(db_session, subject.id, teacher.id, [a.id], None)
    assert exc.value.reason == "too_few"


async def test_scope_and_latest_submission_follow_the_squad(db_session) -> None:
    subject, asg, (a, b, _) = await _subject(db_session)
    teacher = User(username="t2", password_hash="x", role=UserRole.TEACHER)
    db_session.add(teacher)
    await db_session.flush()
    squad = await squads.teacher_assign(db_session, subject.id, teacher.id, [a.id, b.id])
    sub = await _submit(db_session, a, asg, squad_id=squad.id)
    scope = await squads.scope_sa_ids(db_session, b.id, subject.id, asg.id)
    assert len(scope) == 2
    latest = await squads.latest_submission_for(db_session, b.id, subject.id, asg.id)
    assert latest is not None and latest.id == sub.id
    share = await squads.shared_submissions(db_session, subject.id)
    assert share[(b.id, asg.id)].submission.id == sub.id


async def test_quiz_complete_needs_every_member(db_session) -> None:
    from submissions_checker.db.models import QuizAttempt
    from submissions_checker.db.models.enums import QuizAttemptStatus

    subject, asg, (a, b, _) = await _subject(db_session)
    teacher = User(username="t3", password_hash="x", role=UserRole.TEACHER)
    db_session.add(teacher)
    await db_session.flush()
    squad = await squads.teacher_assign(db_session, subject.id, teacher.id, [a.id, b.id])
    sub = await _submit(db_session, a, asg, squad_id=squad.id)

    def attempt(student_id: int, passed: bool) -> QuizAttempt:
        return QuizAttempt(
            submission_id=sub.id,
            student_id=student_id,
            questions_snapshot=[],
            config_snapshot={},
            started_at=datetime.now(UTC),
            status=QuizAttemptStatus.COMPLETED,
            is_passed=passed,
            score=1 if passed else 0,
            max_score=1,
        )

    db_session.add(attempt(a.id, True))
    await db_session.flush()
    assert await squads.quiz_complete(db_session, sub) is False
    db_session.add(attempt(b.id, True))
    await db_session.flush()
    assert await squads.quiz_complete(db_session, sub) is True
