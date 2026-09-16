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
from sqlalchemy import select
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
from submissions_checker.db.models.enums import QuizAttemptStatus, UserRole
from submissions_checker.services.gradebook import (
    fetch_grid_rows,
    fetch_integrity_rows,
    fetch_roster_rows,
)
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
    config: dict | None = None,
) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id,
        code=code,
        title=title,
        min_grade=min_grade,
        max_grade=max_grade,
        deadline=deadline,
        config=config or {},
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


async def test_fetch_integrity_rows_counts_other_event_types(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    """tab_switch/window_blur aren't the only anti-cheat event types (see
    docs/anti-cheat.md): copy_attempt, keyboard_shortcut, right_click, resize,
    fullscreen_exit must still surface as a count and still flag the row/dot,
    even though severity_for stays spec-locked to tab_switch/window_blur/_force_fail."""
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Quiz 1", code="quiz1")
    student = await make_student(full_name="Other Events Student")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=100),
        violations={"copy_attempt": 4, "resize": 2},
    )

    rows = await fetch_integrity_rows(db, subject.id)

    assert len(rows) == 1
    row = rows[0]
    assert row.tab_switch == 0
    assert row.window_blur == 0
    assert row.other_events == 6
    assert row.flagged is True

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert f'data-scroll-to="integrity-row-{student.id}-{a1.id}"' in resp.text


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
    await _make_quiz_attempt(
        db, sub1.id, started_at=start, submitted_at=start + timedelta(seconds=600)
    )
    await _make_quiz_attempt(
        db, sub2.id, started_at=start, submitted_at=start + timedelta(seconds=60)
    )

    rows = await fetch_integrity_rows(db, subject.id)
    by_student = {r.student_name: r for r in rows}
    assert by_student["S1"].median_seconds == 330
    assert by_student["S2"].duration_anomalous is True
    assert by_student["S1"].duration_anomalous is False


async def test_fetch_integrity_rows_excludes_test_students(
    db: AsyncSession, teacher, make_student, make_group
) -> None:
    """A Test Student account (never enrolled via subjects_students — see the
    owner-only "Test Student" panel, which promises stats exclude this account)
    must not show up as a ghost row here, nor pollute the per-assignment median
    used to flag real students' attempts as anomalous."""
    from submissions_checker.db.models.enums import EntityType

    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Quiz 1", code="quiz1")

    student = await make_student(full_name="Real Student")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=100),
        violations={"tab_switch": 3},
    )

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
    # Note: no _enroll(db, subject.id, test_student.id) — mirrors the real
    # Test Student account, which is never in subjects_students.
    test_sa = await _make_student_assignment(db, test_student.id, a1.id)
    test_sub = await _make_submission(db, test_sa.id, status=SubmissionStatus.COMPLETED)
    await _make_quiz_attempt(
        db,
        test_sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=100),
        violations={"tab_switch": 3},
    )

    rows = await fetch_integrity_rows(db, subject.id)
    assert len(rows) == 1
    assert rows[0].student_name == "Real Student"


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


async def test_teacher_subject_has_four_tabs(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    for target in ["overview", "students", "assignments", "grades"]:
        assert f'data-tab-target="{target}"' in resp.text
        assert f'id="tab-panel-{target}"' in resp.text


async def test_grades_tab_renders_stats_and_grid(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="ЛР1", code="lab1", min_grade=50)
    student = await make_student(full_name="Grid Student")
    await _enroll(db, subject.id, student.id)
    await _make_student_assignment(db, student.id, a1.id, grade=90)

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert "ЛР1" in resp.text
    assert "Grid Student" in resp.text
    assert ">90<" in resp.text  # the grade appears as a cell value
    assert "90.0" in resp.text  # average score stat card
    assert "100.0%" in resp.text  # pass rate stat card — the one student passed the one assignment


async def test_integrity_table_shows_severity_and_flag_filter(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Quiz 1", code="quiz1")
    student = await make_student(full_name="Flagged Student")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=100),
        violations={"tab_switch": 3},
    )

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert "Flagged Student" in resp.text
    assert f'id="integrity-row-{student.id}-{a1.id}"' in resp.text
    assert f'id="cell-{student.id}-{a1.id}"' in resp.text
    assert f'data-scroll-to="integrity-row-{student.id}-{a1.id}"' in resp.text
    assert "data-flagged-only-toggle" in resp.text


async def test_grid_cell_has_no_violation_dot_for_a_clean_quiz_attempt(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    """A finalized attempt with empty violations still gets an integrity row
    (it's the full roster, per test_integrity_table_shows_severity_and_flag_filter),
    but a teacher scanning the grid shouldn't see a warning dot on a clean cell."""
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Quiz 1", code="quiz1")
    student = await make_student(full_name="Clean Student")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    sub = await _make_submission(db, sa.id, status=SubmissionStatus.COMPLETED)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    await _make_quiz_attempt(
        db,
        sub.id,
        started_at=start,
        submitted_at=start + timedelta(seconds=600),
        violations={},
    )

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert f'id="integrity-row-{student.id}-{a1.id}"' in resp.text
    # "violation-dot" as a bare substring also appears in the tab-switcher <script>'s
    # querySelectorAll(".violation-dot") selector regardless of any cell rendering one —
    # assert on the specific data-scroll-to attribute a rendered dot span would carry.
    assert f'data-scroll-to="integrity-row-{student.id}-{a1.id}"' not in resp.text


# ── fetch_grid_rows ──────────────────────────────────────────────────────────


async def test_fetch_grid_rows_includes_group_name_and_grade_breakdown(
    db: AsyncSession, teacher, make_student, make_group
) -> None:
    group = await make_group(name="IT-21")
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Lab 1", code="lab1", min_grade=50)
    student = await make_student(full_name="Grid Row Student", group=group)
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id, grade=90)
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=SubmissionStatus.COMPLETED,
        grade_breakdown={"quiz_score": 85.0, "quality_score": 92.0, "works_score": 90.0},
    )
    db.add(sub)
    await db.commit()

    rows = await fetch_grid_rows(db, subject.id)

    assert len(rows) == 1
    row = rows[0]
    assert row.group_name == "IT-21"
    assert row.grade == 90
    assert row.quiz_score == 85.0
    assert row.review_score == 92.0


async def test_fetch_grid_rows_null_grade_breakdown_yields_none_sub_marks(
    db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id)
    student = await make_student()
    await _enroll(db, subject.id, student.id)
    await _make_student_assignment(db, student.id, a1.id)

    rows = await fetch_grid_rows(db, subject.id)

    assert rows[0].quiz_score is None
    assert rows[0].review_score is None


# ── search-by-email + enroll-by-search routes ───────────────────────────────


async def test_search_students_requires_three_characters(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}/students/search?q=ab")
    assert resp.status_code == 422


async def test_search_students_matches_email_substring(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    student = await make_student(full_name="Search Target", email="findme@example.com")
    await make_student(full_name="Nobody", email="other@example.com")
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}/students/search?q=findme")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == student.id
    assert data[0]["email"] == "findme@example.com"


async def test_search_students_other_teachers_subject_is_403(
    client: AsyncClient, db: AsyncSession, teacher, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other-search")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}/students/search?q=abc")
    assert resp.status_code == 403


async def test_enroll_by_search_creates_enrollment(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    await _make_assignment(db, subject.id, code="lab1")
    student = await make_student(full_name="Enroll Me", email="enrollme@example.com")
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/enroll-by-search",
        data={"student_id": str(student.id)},
    )
    assert resp.status_code == 303

    enrollment = (
        await db.execute(
            select(SubjectsStudents).where(
                SubjectsStudents.subject_id == subject.id,
                SubjectsStudents.student_id == student.id,
            )
        )
    ).scalar_one_or_none()
    assert enrollment is not None


async def test_enroll_by_search_requires_variant_when_subject_needs_one(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    await _make_assignment(
        db, subject.id, code="lab1", config={"variants_required": True, "variants": {"a": {}}}
    )
    student = await make_student(email="needsvariant@example.com")
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/enroll-by-search",
        data={"student_id": str(student.id)},
    )
    assert resp.status_code == 422
