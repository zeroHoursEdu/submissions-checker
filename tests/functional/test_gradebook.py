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
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import (
    QuizAttempt,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import QuizAttemptStatus
from submissions_checker.services.gradebook import fetch_integrity_rows, fetch_roster_rows
from tests.functional.conftest import authenticate

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


# ── fetch_integrity_rows ─────────────────────────────────────────────────────


async def _make_quiz_attempt(
    db: AsyncSession,
    submission_id: int,
    *,
    started_at: datetime,
    submitted_at: datetime | None,
    paused_seconds: int = 0,
    violations: dict | None = None,
    status: QuizAttemptStatus = QuizAttemptStatus.COMPLETED,
) -> QuizAttempt:
    attempt = QuizAttempt(
        submission_id=submission_id,
        questions_snapshot=[],
        config_snapshot={},
        started_at=started_at,
        submitted_at=submitted_at,
        paused_seconds=paused_seconds,
        violations=violations or {},
        status=status,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def test_fetch_integrity_rows_computes_severity_and_duration(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Quiz 1", code="quiz1")
    student = await make_student(full_name="Quiz Taker")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=600),
        violations={"tab_switch": 2, "window_blur": 1},
    )

    rows = await fetch_integrity_rows(db, subject.id)

    assert len(rows) == 1
    row = rows[0]
    assert row.student_name == "Quiz Taker"
    assert row.tab_switch == 2
    assert row.window_blur == 1
    assert row.duration_seconds == 600
    assert row.severity == "high"  # combined count 3
    assert row.flagged is True


async def test_fetch_integrity_rows_excludes_unfinished_attempts(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, code="quiz1")
    student = await make_student()
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.QUIZ_SENT)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=datetime.now(UTC),
        submitted_at=None,
        status=QuizAttemptStatus.IN_PROGRESS,
    )

    rows = await fetch_integrity_rows(db, subject.id)
    assert rows == []


async def test_fetch_integrity_rows_median_is_per_assignment(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, code="quiz1")
    s1 = await make_student(full_name="S1", email="s1@example.com")
    s2 = await make_student(full_name="S2", email="s2@example.com")
    await _enroll(db, subject.id, s1.id)
    await _enroll(db, subject.id, s2.id)
    sa1 = await _make_student_assignment(db, s1.id, a1.id)
    sa2 = await _make_student_assignment(db, s2.id, a1.id)
    sub1 = await _make_submission(db, sa1.id, status=SubmissionStatus.COMPLETED)
    sub2 = await _make_submission(db, sa2.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    # S1 took 600s (normal), S2 took 60s -> 60 / median(600,60)=330 is ~18% -> anomalous
    await _make_quiz_attempt(db, sub1.id, started_at=start, submitted_at=start + timedelta(seconds=600))
    await _make_quiz_attempt(db, sub2.id, started_at=start, submitted_at=start + timedelta(seconds=60))

    rows = await fetch_integrity_rows(db, subject.id)
    by_student = {r.student_name: r for r in rows}
    assert by_student["S1"].median_seconds == 330
    assert by_student["S2"].duration_anomalous is True
    assert by_student["S1"].duration_anomalous is False


# ── teacher_subject route wiring (Task 7) ────────────────────────────────────


async def test_teacher_subject_page_includes_gradebook_context(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Lab 1", code="lab1")
    student = await make_student(full_name="Context Student")
    await _enroll(db, subject.id, student.id)
    await _make_student_assignment(db, student.id, a1.id, grade=100)

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert "Context Student" in resp.text


async def test_teacher_subject_default_tab_is_overview_after_enroll_flash(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}?enrolled=1")
    assert resp.status_code == 200
    assert 'data-default-tab="overview"' in resp.text


async def test_teacher_subject_default_tab_is_students_normally(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    assert 'data-default-tab="students"' in resp.text
