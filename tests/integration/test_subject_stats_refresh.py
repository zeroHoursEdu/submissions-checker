"""Integration test for the subject-stats scheduled job — real Postgres via
testcontainers, calling refresh_subject_gradebook_stats() directly (not
through the app), same pattern as test_teacher_digest.py."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from submissions_checker.db.base import Base
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.workers.scheduled import subject_stats_refresh


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


def _patch(monkeypatch, db_session) -> None:
    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(subject_stats_refresh, "get_session", fake_get_session)


@pytest.mark.asyncio
async def test_refresh_writes_one_row_per_subject(db_session: AsyncSession, monkeypatch) -> None:
    group = Group(name="IT-21")
    db_session.add(group)
    await db_session.flush()
    student = Student(group_id=group.id, email="s@e.com", full_name="S")
    subject = Subject(name="Sub", owner_id=None)
    db_session.add_all([student, subject])
    await db_session.flush()
    sa = SubjectsAssignment(subject_id=subject.id, title="A1", min_grade=50, max_grade=100)
    db_session.add(sa)
    await db_session.flush()
    db_session.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    db_session.add(StudentAssignment(student_id=student.id, subjects_assignment_id=sa.id, grade=80))
    await db_session.commit()

    _patch(monkeypatch, db_session)
    await subject_stats_refresh.refresh_subject_gradebook_stats()

    row = (
        await db_session.execute(
            select(SubjectGradebookStats).where(SubjectGradebookStats.subject_id == subject.id)
        )
    ).scalar_one()
    assert row.pass_pct == 100.0
    assert row.average_mark_pct == 80.0
    assert row.pending_review_count == 0


@pytest.mark.asyncio
async def test_refresh_is_idempotent_updates_not_duplicates(
    db_session: AsyncSession, monkeypatch
) -> None:
    subject = Subject(name="Sub2", owner_id=None)
    db_session.add(subject)
    await db_session.commit()

    _patch(monkeypatch, db_session)
    await subject_stats_refresh.refresh_subject_gradebook_stats()
    await subject_stats_refresh.refresh_subject_gradebook_stats()

    rows = (
        (
            await db_session.execute(
                select(SubjectGradebookStats).where(SubjectGradebookStats.subject_id == subject.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
