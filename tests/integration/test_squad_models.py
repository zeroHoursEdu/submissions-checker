"""Schema-level guarantees of the squad tables (real Postgres via db_session)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from submissions_checker.db.models import Group, Student, Subject, SubmissionSourceType
from submissions_checker.db.models.enums import SquadInviteStatus
from submissions_checker.db.models.squad import Squad, SquadInvite, SquadMember
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission

pytestmark = pytest.mark.asyncio


async def _two_students(db_session) -> tuple[Subject, Student, Student]:
    group = Group(name="G-1")
    subject = Subject(name="S", squad_max_size=2)
    db_session.add_all([group, subject])
    await db_session.flush()
    a = Student(group_id=group.id, email="a@x", full_name="A")
    b = Student(group_id=group.id, email="b@x", full_name="B")
    db_session.add_all([a, b])
    await db_session.flush()
    return subject, a, b


async def test_student_cannot_be_in_two_squads_of_one_subject(db_session) -> None:
    subject, a, _ = await _two_students(db_session)
    s1 = Squad(subject_id=subject.id)
    s2 = Squad(subject_id=subject.id)
    db_session.add_all([s1, s2])
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        SquadMember(squad_id=s1.id, student_id=a.id, subject_id=subject.id, joined_at=now)
    )
    await db_session.flush()
    db_session.add(
        SquadMember(squad_id=s2.id, student_id=a.id, subject_id=subject.id, joined_at=now)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_only_one_pending_invite_per_squad_and_student(db_session) -> None:
    subject, a, b = await _two_students(db_session)
    squad = Squad(subject_id=subject.id, created_by_student_id=a.id)
    db_session.add(squad)
    await db_session.flush()
    db_session.add(
        SquadInvite(squad_id=squad.id, invited_student_id=b.id, invited_by_student_id=a.id)
    )
    await db_session.flush()
    db_session.add(
        SquadInvite(squad_id=squad.id, invited_student_id=b.id, invited_by_student_id=a.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_declined_invite_does_not_block_a_new_pending_one(db_session) -> None:
    subject, a, b = await _two_students(db_session)
    squad = Squad(subject_id=subject.id, created_by_student_id=a.id)
    db_session.add(squad)
    await db_session.flush()
    db_session.add(
        SquadInvite(
            squad_id=squad.id,
            invited_student_id=b.id,
            invited_by_student_id=a.id,
            status=SquadInviteStatus.DECLINED,
        )
    )
    await db_session.flush()
    db_session.add(
        SquadInvite(squad_id=squad.id, invited_student_id=b.id, invited_by_student_id=a.id)
    )
    await db_session.flush()  # no error


async def test_submission_and_attempt_carry_squad_columns(db_session) -> None:
    subject, a, _ = await _two_students(db_session)
    squad = Squad(subject_id=subject.id)
    asg = SubjectsAssignment(subject_id=subject.id, title="L1", code="l1", config={})
    db_session.add_all([squad, asg])
    await db_session.flush()
    sa = StudentAssignment(student_id=a.id, subjects_assignment_id=asg.id)
    db_session.add(sa)
    await db_session.flush()
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        squad_id=squad.id,
    )
    db_session.add(sub)
    await db_session.flush()
    assert sub.squad_id == squad.id
    assert subject.squad_max_size == 2
