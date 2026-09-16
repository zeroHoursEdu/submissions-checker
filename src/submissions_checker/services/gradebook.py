"""Read-only gradebook + quiz-integrity aggregation for the teacher subject page.

Everything here is computed once per page load in `teacher_portal.teacher_subject`
and handed to the template pre-shaped — no per-row recomputation in Jinja or JS.

Split the way `services.grading` splits `compute_grade` (pure) from
`finalize_grade` (DB-touching): the classification rules below (`cell_status`,
`severity_for`, `duration_anomalous`, `median_duration`) and the aggregations
built on top of them (`compute_stats`, `build_grid`) are pure and DB-free;
`fetch_roster_rows` / `fetch_integrity_rows` are the only two functions that
touch the database.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import (
    EntityType,
    QuizAttempt,
    Student,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import SubmissionStatus

_TERMINAL_STATUSES = {SubmissionStatus.COMPLETED, SubmissionStatus.FAILED}

CellStatus = Literal["passed", "failed", "pending", "not_submitted"]
Severity = Literal["high", "medium"]


@dataclass(frozen=True)
class RosterRow:
    """One (student, assignment) pair — the shared base dataset for stats + grid."""

    student_id: int
    student_name: str
    assignment_id: int
    assignment_code: str | None
    assignment_title: str
    min_grade: int
    max_grade: int
    deadline: datetime | None
    student_assignment_id: int | None
    grade: int | None
    submission_status: SubmissionStatus | None


@dataclass(frozen=True)
class GradebookStats:
    average_score: float | None
    pass_rate_pct: float
    overdue_count: int
    pending_review_count: int


@dataclass(frozen=True)
class GradebookCell:
    student_assignment_id: int | None
    grade: int | None
    status: CellStatus


@dataclass(frozen=True)
class GradebookColumn:
    assignment_id: int
    title: str
    max_grade: int


@dataclass(frozen=True)
class GradebookRow:
    student_id: int
    student_name: str
    cells: dict[int, GradebookCell]
    total: int | None


@dataclass(frozen=True)
class GradebookGrid:
    columns: list[GradebookColumn]
    rows: list[GradebookRow]


@dataclass(frozen=True)
class IntegrityRow:
    student_id: int
    student_name: str
    assignment_id: int
    assignment_title: str
    tab_switch: int
    window_blur: int
    force_fail: bool
    duration_seconds: int | None
    median_seconds: int | None
    duration_anomalous: bool
    severity: Severity | None
    flagged: bool


def cell_status(
    grade: int | None, min_grade: int, submission_status: SubmissionStatus | None
) -> CellStatus:
    """Richer than "passed" elsewhere in this module: reflects grade quality.

    grade present            -> passed/failed against this assignment's min_grade
    no grade, submission mid-pipeline -> pending
    no grade, no submission or a terminal one with no grade -> not_submitted
    """
    if grade is not None:
        return "passed" if grade >= min_grade else "failed"
    if submission_status is not None and submission_status not in _TERMINAL_STATUSES:
        return "pending"
    return "not_submitted"


def severity_for(*, force_fail: bool, tab_switch: int, window_blur: int) -> Severity | None:
    combined = tab_switch + window_blur
    if force_fail or combined >= 3:
        return "high"
    if 1 <= combined <= 2:
        return "medium"
    return None


def duration_anomalous(duration_seconds: int | None, median_seconds: int | None) -> bool:
    if duration_seconds is None or median_seconds is None or median_seconds <= 0:
        return False
    return duration_seconds < 0.3 * median_seconds


def median_duration(durations: list[int]) -> int | None:
    if not durations:
        return None
    return round(statistics.median(durations))


def compute_stats(rows: list[RosterRow], *, now: datetime) -> GradebookStats:
    graded = [r.grade for r in rows if r.grade is not None]
    average_score = round(sum(graded) / len(graded), 1) if graded else None

    by_student: dict[int, list[RosterRow]] = {}
    for r in rows:
        by_student.setdefault(r.student_id, []).append(r)
    total_students = len(by_student)
    passed_students = sum(
        1 for student_rows in by_student.values() if all(r.grade is not None for r in student_rows)
    )
    pass_rate_pct = round(100 * passed_students / total_students, 1) if total_students else 0.0

    overdue_count = sum(
        1 for r in rows if r.grade is None and r.deadline is not None and r.deadline < now
    )
    pending_review_count = sum(
        1
        for r in rows
        if r.grade is None
        and r.submission_status is not None
        and r.submission_status not in _TERMINAL_STATUSES
    )
    return GradebookStats(
        average_score=average_score,
        pass_rate_pct=pass_rate_pct,
        overdue_count=overdue_count,
        pending_review_count=pending_review_count,
    )


def build_grid(rows: list[RosterRow]) -> GradebookGrid:
    columns: list[GradebookColumn] = []
    seen_assignments: set[int] = set()
    for r in rows:
        if r.assignment_id not in seen_assignments:
            seen_assignments.add(r.assignment_id)
            columns.append(GradebookColumn(r.assignment_id, r.assignment_title, r.max_grade))

    cells_by_student: dict[int, dict[int, GradebookCell]] = {}
    names_by_student: dict[int, str] = {}
    for r in rows:
        cells_by_student.setdefault(r.student_id, {})[r.assignment_id] = GradebookCell(
            r.student_assignment_id,
            r.grade,
            cell_status(r.grade, r.min_grade, r.submission_status),
        )
        names_by_student[r.student_id] = r.student_name

    grid_rows = []
    for student_id, cells in cells_by_student.items():
        grades = [c.grade for c in cells.values() if c.grade is not None]
        total = sum(grades) if grades else None
        grid_rows.append(GradebookRow(student_id, names_by_student[student_id], cells, total))
    grid_rows.sort(key=lambda row: row.student_name)

    return GradebookGrid(columns=columns, rows=grid_rows)


async def fetch_roster_rows(db: AsyncSession, subject_id: int) -> list[RosterRow]:
    """One row per (enrolled real student, subject assignment) pair.

    Reuses the "latest submission per student_assignment" subquery pattern from
    ``teacher_portal.teacher_assignment`` (there scoped to one assignment; here
    scoped to every assignment in the subject at once).
    """
    latest_sub_sq = (
        select(
            Submission.students_assignment_id,
            func.max(Submission.created_at).label("max_created_at"),
        )
        .group_by(Submission.students_assignment_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name.label("student_name"),
            SubjectsAssignment.id.label("assignment_id"),
            SubjectsAssignment.code.label("assignment_code"),
            SubjectsAssignment.title.label("assignment_title"),
            SubjectsAssignment.min_grade,
            SubjectsAssignment.max_grade,
            SubjectsAssignment.deadline,
            StudentAssignment.id.label("student_assignment_id"),
            StudentAssignment.grade,
            Submission.status.label("submission_status"),
        )
        .select_from(SubjectsStudents)
        .join(Student, Student.id == SubjectsStudents.student_id)
        .join(SubjectsAssignment, SubjectsAssignment.subject_id == SubjectsStudents.subject_id)
        .outerjoin(
            StudentAssignment,
            and_(
                StudentAssignment.student_id == SubjectsStudents.student_id,
                StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
            ),
        )
        .outerjoin(latest_sub_sq, latest_sub_sq.c.students_assignment_id == StudentAssignment.id)
        .outerjoin(
            Submission,
            and_(
                Submission.students_assignment_id == StudentAssignment.id,
                Submission.created_at == latest_sub_sq.c.max_created_at,
            ),
        )
        .where(SubjectsStudents.subject_id == subject_id, Student.type == EntityType.REAL)
        .order_by(
            Student.full_name,
            SubjectsAssignment.deadline.asc().nullslast(),
            SubjectsAssignment.id,
        )
    )
    return [
        RosterRow(
            student_id=row.student_id,
            student_name=row.student_name,
            assignment_id=row.assignment_id,
            assignment_code=row.assignment_code,
            assignment_title=row.assignment_title,
            min_grade=row.min_grade,
            max_grade=row.max_grade,
            deadline=row.deadline,
            student_assignment_id=row.student_assignment_id,
            grade=row.grade,
            submission_status=row.submission_status,
        )
        for row in result
    ]


async def fetch_integrity_rows(db: AsyncSession, subject_id: int) -> list[IntegrityRow]:
    """One row per finalized QuizAttempt, latest attempt per (student, assignment).

    "Latest" matches the same simplification `teacher_assignment`'s violation_flags
    already makes (teacher_portal.py, the `viol_result` block): when a student has
    retried a quiz, the most recent attempt is the one shown. This is also the
    exact attempt the gradebook cell's violation indicator (Task 9) points at, so
    a click on the cell always finds a matching row here.
    """
    result = await db.execute(
        select(
            Student.id.label("student_id"),
            Student.full_name.label("student_name"),
            SubjectsAssignment.id.label("assignment_id"),
            SubjectsAssignment.title.label("assignment_title"),
            QuizAttempt.started_at,
            QuizAttempt.submitted_at,
            QuizAttempt.paused_seconds,
            QuizAttempt.violations,
        )
        .select_from(QuizAttempt)
        .join(Submission, Submission.id == QuizAttempt.submission_id)
        .join(StudentAssignment, StudentAssignment.id == Submission.students_assignment_id)
        .join(
            SubjectsAssignment,
            SubjectsAssignment.id == StudentAssignment.subjects_assignment_id,
        )
        .join(Student, Student.id == StudentAssignment.student_id)
        .where(
            SubjectsAssignment.subject_id == subject_id,
            QuizAttempt.submitted_at.is_not(None),
        )
        .order_by(QuizAttempt.started_at.desc())
    )
    raw_rows = list(result)

    latest_by_pair: dict[tuple[int, int], Any] = {}
    for row in raw_rows:
        key = (row.student_id, row.assignment_id)
        if key not in latest_by_pair:  # rows are started_at DESC -> first hit is latest
            latest_by_pair[key] = row

    durations_by_assignment: dict[int, list[int]] = {}
    with_duration = []
    for row in latest_by_pair.values():
        elapsed = (row.submitted_at - row.started_at).total_seconds()
        duration = int(elapsed) - row.paused_seconds
        durations_by_assignment.setdefault(row.assignment_id, []).append(duration)
        with_duration.append((row, duration))

    medians = {
        assignment_id: median_duration(durations)
        for assignment_id, durations in durations_by_assignment.items()
    }

    integrity_rows = []
    for row, duration in with_duration:
        violations = row.violations or {}
        tab_switch = int(violations.get("tab_switch", 0))
        window_blur = int(violations.get("window_blur", 0))
        force_fail = bool(violations.get("_force_fail", False))
        median = medians[row.assignment_id]
        anomalous = duration_anomalous(duration, median)
        severity = severity_for(
            force_fail=force_fail, tab_switch=tab_switch, window_blur=window_blur
        )
        integrity_rows.append(
            IntegrityRow(
                student_id=row.student_id,
                student_name=row.student_name,
                assignment_id=row.assignment_id,
                assignment_title=row.assignment_title,
                tab_switch=tab_switch,
                window_blur=window_blur,
                force_fail=force_fail,
                duration_seconds=duration,
                median_seconds=median,
                duration_anomalous=anomalous,
                severity=severity,
                flagged=(severity is not None) or anomalous,
            )
        )
    integrity_rows.sort(key=lambda r: (r.student_name, r.assignment_title))
    return integrity_rows
