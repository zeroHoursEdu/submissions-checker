"""Integration tests for the deadline-reminder scheduled job — real Postgres via
testcontainers, same harness as test_subject_stats_refresh.py."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from submissions_checker.db.base import Base
from submissions_checker.db.models import (
    Group,
    NotificationPreference,
    OutboxMessage,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import (
    NotificationCase,
    NotificationMethod,
    OutboxEventType,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.workers.scheduled import deadline_reminders
from submissions_checker.workers.scheduled.deadline_reminders import (
    enqueue_due_deadline_reminders,
    run_deadline_reminders,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db_session(test_settings):
    engine: AsyncEngine = create_async_engine(str(test_settings.database_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _arrange(
    db: AsyncSession, *, deadline: datetime, students: int = 2, tag: str = "a"
) -> tuple[SubjectsAssignment, list[Student]]:
    group = Group(name=f"IT-{tag}")
    db.add(group)
    await db.flush()
    subject = Subject(name="Sub", owner_id=None)
    db.add(subject)
    await db.flush()
    sa = SubjectsAssignment(subject_id=subject.id, title="A1", code="a1", deadline=deadline)
    db.add(sa)
    await db.flush()
    rows = []
    for i in range(students):
        s = Student(group_id=group.id, email=f"s{i}-{tag}@e.com", full_name=f"S{i}")
        db.add(s)
        await db.flush()
        db.add(SubjectsStudents(subject_id=subject.id, student_id=s.id))
        db.add(StudentAssignment(student_id=s.id, subjects_assignment_id=sa.id))
        rows.append(s)
    await db.commit()
    return sa, rows


async def _reminders(db: AsyncSession) -> list[OutboxMessage]:
    return list(
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.event_type == OutboxEventType.DEADLINE_REMINDER
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_enqueues_one_reminder_per_unsubmitted_student(db_session: AsyncSession) -> None:
    sa, (a, b) = await _arrange(db_session, deadline=NOW + timedelta(days=1))
    sa_row = (
        await db_session.execute(
            select(StudentAssignment).where(
                StudentAssignment.student_id == a.id,
                StudentAssignment.subjects_assignment_id == sa.id,
            )
        )
    ).scalar_one()
    db_session.add(
        Submission(
            students_assignment_id=sa_row.id,
            source_type=SubmissionSourceType.ZIP_UPLOAD,
            source_metadata={},
            status=SubmissionStatus.PENDING,
        )
    )
    await db_session.commit()

    n = await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2)

    assert n == 1
    rows = await _reminders(db_session)
    assert len(rows) == 1
    assert rows[0].payload["student_id"] == b.id
    assert rows[0].payload["subjects_assignment_id"] == sa.id
    assert rows[0].payload["deadline_str"].startswith("2026-09-18")


@pytest.mark.asyncio
async def test_is_idempotent(db_session: AsyncSession) -> None:
    await _arrange(db_session, deadline=NOW + timedelta(days=1), students=1)
    assert await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2) == 1
    assert await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2) == 0
    assert len(await _reminders(db_session)) == 1


@pytest.mark.asyncio
async def test_skips_far_and_past_deadlines(db_session: AsyncSession) -> None:
    await _arrange(db_session, deadline=NOW + timedelta(days=10), students=1, tag="far")
    assert await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2) == 0
    await _arrange(db_session, deadline=NOW - timedelta(days=1), students=1, tag="past")
    assert await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2) == 0


@pytest.mark.asyncio
async def test_skips_students_who_opted_out(db_session: AsyncSession) -> None:
    _sa, (only,) = await _arrange(db_session, deadline=NOW + timedelta(days=1), students=1)
    db_session.add(
        NotificationPreference(
            student_id=only.id,
            case=NotificationCase.DEADLINE_REMINDER,
            method=NotificationMethod.EMAIL,
            enabled=False,
        )
    )
    await db_session.commit()
    assert await enqueue_due_deadline_reminders(db_session, now=NOW, days_before=2) == 0


@pytest.mark.asyncio
async def test_job_entry_point_runs_under_lock(db_session: AsyncSession, monkeypatch) -> None:
    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(deadline_reminders, "get_session", fake_get_session)
    await _arrange(db_session, deadline=datetime.now(UTC) + timedelta(days=1), students=1)
    await run_deadline_reminders()
    assert len(await _reminders(db_session)) == 1
