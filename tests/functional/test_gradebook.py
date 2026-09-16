"""DB-backed tests for services.gradebook's query functions and the resulting
teacher_subject render.

Pure classification/aggregation logic (severity, duration anomaly, cell status,
compute_stats, build_grid) is covered without a database in
tests/unit/test_gradebook.py; this file only exercises the two functions that
touch Postgres, plus the route wiring in Task 7 onward. Fixture helpers mirror
tests/functional/test_portal_detail_pages.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import (
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.services.gradebook import fetch_roster_rows

pytestmark = pytest.mark.asyncio


# ── Arrange helpers (mirrors test_portal_detail_pages.py) ───────────────────


async def _make_subject(
    db: AsyncSession, owner_id: int, *, name: str = "Gradebook Subject"
) -> Subject:
    subject = Subject(name=name, owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


async def _make_assignment(
    db: AsyncSession,
    subject_id: int,
    *,
    title: str = "A1",
    code: str = "a1",
    min_grade: int = 0,
    max_grade: int = 100,
    deadline: datetime | None = None,
) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id,
        code=code,
        title=title,
        min_grade=min_grade,
        max_grade=max_grade,
        deadline=deadline,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _enroll(db: AsyncSession, subject_id: int, student_id: int) -> None:
    db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
    await db.commit()


async def _make_student_assignment(
    db: AsyncSession, student_id: int, sa_id: int, *, grade: int | None = None
) -> StudentAssignment:
    sa = StudentAssignment(student_id=student_id, subjects_assignment_id=sa_id, grade=grade)
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _make_submission(
    db: AsyncSession,
    student_assignment_id: int,
    *,
    status: SubmissionStatus = SubmissionStatus.PENDING,
    created_at: datetime | None = None,
) -> Submission:
    sub = Submission(
        students_assignment_id=student_assignment_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=status,
    )
    if created_at is not None:
        sub.created_at = created_at
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


# ── fetch_roster_rows ────────────────────────────────────────────────────────


async def test_fetch_roster_rows_one_row_per_student_assignment_pair(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Lab 1", code="lab1")
    a2 = await _make_assignment(db, subject.id, title="Lab 2", code="lab2")
    student = await make_student(full_name="Roster Student")
    await _enroll(db, subject.id, student.id)
    await _make_student_assignment(db, student.id, a1.id, grade=90)
    # No StudentAssignment row at all for a2 — must still appear as a null-grade pair.

    rows = await fetch_roster_rows(db, subject.id)

    assert len(rows) == 2
    by_assignment = {r.assignment_id: r for r in rows}
    assert by_assignment[a1.id].grade == 90
    assert by_assignment[a2.id].grade is None
    assert by_assignment[a2.id].student_assignment_id is None


async def test_fetch_roster_rows_excludes_test_students(
    db: AsyncSession, teacher, make_student, make_group
) -> None:
    from submissions_checker.db.models.enums import EntityType

    subject = await _make_subject(db, owner_id=teacher.id)
    await _make_assignment(db, subject.id)
    group = await make_group()
    test_student = Student(
        group_id=group.id,
        email="test-student@internal",
        full_name="Test Student",
        type=EntityType.TEST,
    )
    db.add(test_student)
    await db.commit()
    await db.refresh(test_student)
    await _enroll(db, subject.id, test_student.id)

    rows = await fetch_roster_rows(db, subject.id)
    assert rows == []


async def test_fetch_roster_rows_uses_latest_submission_status(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id)
    student = await make_student()
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    now = datetime.now(UTC)
    await _make_submission(
        db, sa.id, status=SubmissionStatus.FAILED, created_at=now - timedelta(hours=1)
    )
    await _make_submission(
        db, sa.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW, created_at=now
    )

    rows = await fetch_roster_rows(db, subject.id)
    assert rows[0].submission_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
