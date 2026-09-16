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
from typing import Literal

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
