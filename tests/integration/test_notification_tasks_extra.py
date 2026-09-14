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
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    SubmissionStatus,
)
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.user import User
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
