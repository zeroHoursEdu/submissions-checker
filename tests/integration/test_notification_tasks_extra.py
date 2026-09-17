"""Extra integration coverage for notification_tasks (DEADLINE_REMINDER branch).

Mirrors tests/integration/test_worker_tasks.py (which does NOT cover
DEADLINE_REMINDER): seeds a real student -> assignment chain on the
testcontainer Postgres, drives the real outbox processor, and asserts the
deadline-reminder email is dispatched / suppressed correctly. The notification
dispatcher is mocked so no real email is sent.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.notification import Notification
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.user import User
from submissions_checker.services import squads
from submissions_checker.workers.scheduled import outbox_processor
from submissions_checker.workers.tasks import notification_tasks


class _FakeDispatcher:
    """Records sends instead of emailing."""

    def __init__(self, with_channel: bool = True) -> None:
        self._channels = [object()] if with_channel else []
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


def _patch_processor_session(monkeypatch, db_session: AsyncSession) -> None:
    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(outbox_processor, "get_session", fake_get_session)


async def _process(db_session: AsyncSession, monkeypatch, message: OutboxMessage) -> OutboxMessage:
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    _patch_processor_session(monkeypatch, db_session)
    await outbox_processor.process_outbox_messages()
    await db_session.refresh(message)
    return message


async def _seed_enrollment(db: AsyncSession, suffix: str) -> tuple[Student, SubjectsAssignment]:
    """Create student + subject + assignment + enrollment; return (student, sa)."""
    group = Group(name=f"grp-{suffix}")
    db.add(group)
    await db.flush()

    student = Student(group_id=group.id, email=f"dl-{suffix}@e.com", full_name=f"Dl {suffix}")
    teacher = User(username=f"dlt-{suffix}", password_hash="x", role="TEACHER", is_active=True)
    db.add_all([student, teacher])
    await db.flush()

    subject = Subject(name=f"DlSub {suffix}", owner_id=teacher.id)
    db.add(subject)
    await db.flush()

    sa = SubjectsAssignment(subject_id=subject.id, title=f"Assignment {suffix}", code="lab1")
    db.add(sa)
    await db.flush()

    enrollment = StudentAssignment(student_id=student.id, subjects_assignment_id=sa.id)
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return student, sa


@pytest.mark.asyncio
async def test_deadline_reminder_sends_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """DEADLINE_REMINDER dispatches a reminder email carrying the deadline."""
    student, sa = await _seed_enrollment(db_session, "send")
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(notification_tasks, "build_dispatcher", lambda _s: dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.DEADLINE_REMINDER,
        payload={
            "student_id": student.id,
            "subjects_assignment_id": sa.id,
            "deadline_str": "2026-07-01 23:59",
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert len(dispatcher.sent) == 1
    recipient, subject, body = dispatcher.sent[0]
    assert recipient == student.email
    assert "Assignment send" in subject
    assert "2026-07-01 23:59" in body


@pytest.mark.asyncio
async def test_deadline_reminder_no_channel_skips(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """With no configured channel the reminder completes without delivering."""
    student, sa = await _seed_enrollment(db_session, "noch")
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher(with_channel=False)
    monkeypatch.setattr(notification_tasks, "build_dispatcher", lambda _s: dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.DEADLINE_REMINDER,
        payload={
            "student_id": student.id,
            "subjects_assignment_id": sa.id,
            "deadline_str": "2026-07-01",
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_deadline_reminder_missing_enrollment_noops(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """A reminder for an unknown enrollment finishes without sending."""
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(notification_tasks, "build_dispatcher", lambda _s: dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.DEADLINE_REMINDER,
        payload={
            "student_id": 999999,
            "subjects_assignment_id": 888888,
            "deadline_str": "2026-07-01",
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


# ── SUBMISSION_REVIEWED squad fan-out (I5) ──────────────────────────────────────


async def _seed_squad_submission(
    db: AsyncSession, suffix: str
) -> tuple[Student, Student, Submission, SubjectsAssignment]:
    """Two enrolled students, teacher-assigned into a locked squad, sharing one
    submission that carries only the uploader's (s1's) StudentAssignment row."""
    group = Group(name=f"sq-grp-{suffix}")
    db.add(group)
    await db.flush()

    s1 = Student(group_id=group.id, email=f"sq1-{suffix}@e.com", full_name=f"Sq1 {suffix}")
    s2 = Student(group_id=group.id, email=f"sq2-{suffix}@e.com", full_name=f"Sq2 {suffix}")
    teacher = User(username=f"sqt-{suffix}", password_hash="x", role="TEACHER", is_active=True)
    db.add_all([s1, s2, teacher])
    await db.flush()

    subject = Subject(name=f"SqSub {suffix}", owner_id=teacher.id, squad_max_size=2)
    db.add(subject)
    await db.flush()

    sa_tmpl = SubjectsAssignment(subject_id=subject.id, title=f"Assignment {suffix}", code="lab1")
    db.add(sa_tmpl)
    await db.flush()

    for s in (s1, s2):
        db.add(SubjectsStudents(subject_id=subject.id, student_id=s.id))
        db.add(StudentAssignment(student_id=s.id, subjects_assignment_id=sa_tmpl.id))
    await db.flush()
    await db.commit()

    squad = await squads.teacher_assign(db, subject.id, teacher.id, [s1.id, s2.id])
    await db.commit()

    sa1_id = await db.scalar(
        select(StudentAssignment.id).where(
            StudentAssignment.student_id == s1.id,
            StudentAssignment.subjects_assignment_id == sa_tmpl.id,
        )
    )
    submission = Submission(
        students_assignment_id=sa1_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        status=SubmissionStatus.AWAITING_TEACHER_REVIEW,
        source_metadata={},
        squad_id=squad.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return s1, s2, submission, sa_tmpl


async def _make_student_user(db: AsyncSession, student: Student) -> User:
    user = User(
        username=f"sq-login-{student.id}", password_hash="x", role="STUDENT", student_id=student.id
    )
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_submission_reviewed_fans_out_to_every_squad_member(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """I5: a squad's shared submission is reviewed once, but every member gets their
    own email + in-app notification, and their own portal link (their own SA id, not
    the uploader's whose SA the submission row happens to carry)."""
    s1, s2, submission, sa_tmpl = await _seed_squad_submission(db_session, "fan")
    u1 = await _make_student_user(db_session, s1)
    u2 = await _make_student_user(db_session, s2)
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(notification_tasks, "build_dispatcher", lambda _s: dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": submission.id, "action": "approve", "reason": ""},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert {r for r, _, _ in dispatcher.sent} == {s1.email, s2.email}

    notifs = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id.in_([u1.id, u2.id]))
            )
        )
        .scalars()
        .all()
    )
    assert len(notifs) == 2

    sa2_id = await db_session.scalar(
        select(StudentAssignment.id).where(
            StudentAssignment.student_id == s2.id,
            StudentAssignment.subjects_assignment_id == sa_tmpl.id,
        )
    )
    s2_notif = next(n for n in notifs if n.user_id == u2.id)
    assert f"/assignments/{sa2_id}" in (s2_notif.link or "")
