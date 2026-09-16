# Gradebook + Integrity Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure `/teacher/subjects/{id}` into tabs (Огляд/Студенти/Завдання/Оцінки), and build a read-only gradebook (server-computed stats + per-assignment grid) plus a quiz-integrity/violations view inside the new Оцінки tab.

**Architecture:** One new pure-query-and-aggregation module (`services/gradebook.py`) computes everything the existing `teacher_subject` route needs in a single extra pair of DB round-trips; the route passes the results into an extended `teacher_subject.html` template. No new routes, no AJAX — matches the codebase's existing single-request Jinja2 pattern.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, PostgreSQL (JSONB), Jinja2, Tailwind (CDN), vanilla JS. Test stack: pytest + pytest-asyncio, real Postgres via the `tests/functional` transactional-DB harness.

**Spec:** `docs/superpowers/specs/2026-09-16-gradebook-integrity-tab-design.md`

## Global Constraints

- No CSV export anywhere in this feature.
- No inline editing of marks or violation data — everything is read-only.
- Do not change the existing feedback-request button's behavior; it stays in the header, outside tab content.
- "Passed" (assignment level) = `StudentAssignment.grade IS NOT NULL` — presence of any mark, not a value check.
- **Do not `git commit` anything from this plan until the user has run the app locally and explicitly signs off.** Every task still ends with its tests passing and the working tree in a good state, but skip the "commit" step described in the general task-structure guidance below — stage nothing, commit nothing — until the final task, which is itself gated on the user's go-ahead (see Task 11).

---

## File Structure

| File | Responsibility |
|---|---|
| `templates/teacher_subject.html` | Tabs, moved sections, new gradebook/integrity markup, `?` fallback fix |
| `src/submissions_checker/api/routes/teacher_portal.py` | `teacher_subject()` calls the new service functions and passes their output + `default_tab` into the template |
| `src/submissions_checker/services/gradebook.py` | New. Dataclasses + pure aggregation functions + the two DB query functions |
| `i18n/uk.yml` | New `teacher.*` keys for tab labels, stat cards, grid, integrity table |
| `tests/unit/test_gradebook.py` | New. Unit tests for the pure functions (`cell_status`, `severity_for`, `duration_anomalous`, `median_duration`, `compute_stats`, `build_grid`) — no DB |
| `tests/functional/test_gradebook.py` | New. DB-backed tests for `fetch_roster_rows` / `fetch_integrity_rows` and route-level rendering of the new tab content |

---

### Task 1: Defensive `?` fallback for a null group name

**Files:**
- Modify: `templates/teacher_subject.html:140`

**Interfaces:** None — template-only change, no new data flows through this task.

This one is not TDD'd: `students.group_id` is `NOT NULL` (`src/submissions_checker/db/models/student.py:25`) and `groups.name` is also `NOT NULL` (`src/submissions_checker/db/models/group.py:22`), and the query at `teacher_portal.py:290-296` uses an inner join on `Group`. A null `group_name` cannot currently occur through any code path or test fixture — there is nothing to write a failing test against. This is a defensive guard against a future schema/query change, not a bugfix for a reachable bug.

- [ ] **Step 1: Edit the template**

In `templates/teacher_subject.html`, find:

```html
              <p class="text-xs text-slate-400 truncate">{{ item.group_name }} · @{{ item.student.github_username }}</p>
```

Replace with:

```html
              <p class="text-xs text-slate-400 truncate">{{ item.group_name or "?" }} · @{{ item.student.github_username }}</p>
```

- [ ] **Step 2: Sanity check**

Run: `docker compose exec app python -c "import jinja2; jinja2.Environment().from_string('{{ x or \"?\" }}').render(x=None)"`
Expected: prints `?` with no error — confirms the Jinja `or` fallback behaves as intended before relying on it in the full template.

---

### Task 2: `gradebook.py` — dataclasses and pure per-cell/per-attempt functions

**Files:**
- Create: `src/submissions_checker/services/gradebook.py`
- Test: `tests/unit/test_gradebook.py`

**Interfaces:**
- Produces: `RosterRow` (dataclass), `GradebookStats`, `GradebookCell`, `GradebookColumn`, `GradebookRow`, `GradebookGrid`, `IntegrityRow` (all dataclasses); `cell_status(grade, min_grade, submission_status) -> Literal["passed","failed","pending","not_submitted"]`; `severity_for(*, force_fail, tab_switch, window_blur) -> Literal["high","medium"] | None`; `duration_anomalous(duration_seconds, median_seconds) -> bool`; `median_duration(durations: list[int]) -> int | None`. All consumed by Tasks 3, 4, 5, 6.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_gradebook.py`:

```python
"""Unit tests for the pure gradebook/integrity rules (services.gradebook).

No database: these are the classification rules a teacher-facing grid and an
integrity table apply to already-fetched rows. DB-touching query functions
(`fetch_roster_rows`, `fetch_integrity_rows`) are covered separately in
tests/functional/test_gradebook.py, matching how services.grading splits
`compute_grade` (unit) from `finalize_grade` (functional).
"""

from __future__ import annotations

from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services.gradebook import (
    cell_status,
    duration_anomalous,
    median_duration,
    severity_for,
)


def test_cell_status_graded_at_or_above_min_is_passed() -> None:
    assert cell_status(grade=50, min_grade=50, submission_status=None) == "passed"


def test_cell_status_graded_below_min_is_failed() -> None:
    assert cell_status(grade=40, min_grade=50, submission_status=None) == "failed"


def test_cell_status_ungraded_with_pending_submission_is_pending() -> None:
    status = cell_status(
        grade=None, min_grade=0, submission_status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    assert status == "pending"


def test_cell_status_ungraded_with_terminal_submission_is_not_submitted() -> None:
    # A FAILED submission with no grade set is not "pending" — the pipeline is done.
    status = cell_status(grade=None, min_grade=0, submission_status=SubmissionStatus.FAILED)
    assert status == "not_submitted"


def test_cell_status_no_submission_at_all_is_not_submitted() -> None:
    assert cell_status(grade=None, min_grade=0, submission_status=None) == "not_submitted"


def test_severity_force_fail_is_high_regardless_of_counts() -> None:
    result = severity_for(force_fail=True, tab_switch=0, window_blur=0)
    assert result == "high"


def test_severity_three_combined_violations_is_high() -> None:
    result = severity_for(force_fail=False, tab_switch=2, window_blur=1)
    assert result == "high"


def test_severity_two_combined_violations_is_medium() -> None:
    result = severity_for(force_fail=False, tab_switch=1, window_blur=1)
    assert result == "medium"


def test_severity_one_violation_is_medium() -> None:
    result = severity_for(force_fail=False, tab_switch=1, window_blur=0)
    assert result == "medium"


def test_severity_zero_violations_is_none() -> None:
    assert severity_for(force_fail=False, tab_switch=0, window_blur=0) is None


def test_duration_anomalous_under_30_percent_of_median() -> None:
    assert duration_anomalous(duration_seconds=50, median_seconds=200) is True


def test_duration_anomalous_at_exactly_30_percent_is_not_anomalous() -> None:
    assert duration_anomalous(duration_seconds=60, median_seconds=200) is False


def test_duration_anomalous_missing_median_is_false() -> None:
    assert duration_anomalous(duration_seconds=10, median_seconds=None) is False


def test_duration_anomalous_missing_duration_is_false() -> None:
    assert duration_anomalous(duration_seconds=None, median_seconds=200) is False


def test_median_duration_odd_count() -> None:
    assert median_duration([100, 300, 200]) == 200


def test_median_duration_even_count_rounds() -> None:
    # median of [100, 201] is 150.5 -> rounds to 150 (banker's rounding on .5 is fine here)
    assert median_duration([100, 201]) == 150 or median_duration([100, 201]) == 151


def test_median_duration_empty_list_is_none() -> None:
    assert median_duration([]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_gradebook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'submissions_checker.services.gradebook'`

- [ ] **Step 3: Create the module with dataclasses and these functions**

Create `src/submissions_checker/services/gradebook.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_gradebook.py -v`
Expected: PASS (16 tests)

---

### Task 3: `compute_stats` — the four stat-card numbers

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/unit/test_gradebook.py`

**Interfaces:**
- Consumes: `RosterRow` (Task 2)
- Produces: `compute_stats(rows: list[RosterRow], *, now: datetime) -> GradebookStats`. Consumed by Task 7 (route wiring).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_gradebook.py`:

```python
from datetime import UTC, datetime, timedelta

from submissions_checker.services.gradebook import RosterRow, compute_stats

_NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _row(
    *,
    student_id: int = 1,
    assignment_id: int = 1,
    grade: int | None = None,
    deadline: datetime | None = None,
    submission_status: SubmissionStatus | None = None,
    min_grade: int = 0,
    max_grade: int = 100,
) -> RosterRow:
    return RosterRow(
        student_id=student_id,
        student_name=f"Student {student_id}",
        assignment_id=assignment_id,
        assignment_code=f"a{assignment_id}",
        assignment_title=f"Assignment {assignment_id}",
        min_grade=min_grade,
        max_grade=max_grade,
        deadline=deadline,
        student_assignment_id=student_id * 100 + assignment_id,
        grade=grade,
        submission_status=submission_status,
    )


def test_compute_stats_average_score_over_graded_only() -> None:
    rows = [_row(grade=80), _row(grade=None), _row(grade=60, assignment_id=2)]
    stats = compute_stats(rows, now=_NOW)
    assert stats.average_score == 70.0


def test_compute_stats_average_score_none_when_nothing_graded() -> None:
    rows = [_row(grade=None)]
    assert compute_stats(rows, now=_NOW).average_score is None


def test_compute_stats_pass_rate_requires_every_assignment_graded() -> None:
    # Student 1 graded on both assignments -> passed. Student 2 graded on only one -> not.
    rows = [
        _row(student_id=1, assignment_id=1, grade=50),
        _row(student_id=1, assignment_id=2, grade=50),
        _row(student_id=2, assignment_id=1, grade=50),
        _row(student_id=2, assignment_id=2, grade=None),
    ]
    stats = compute_stats(rows, now=_NOW)
    assert stats.pass_rate_pct == 50.0


def test_compute_stats_pass_rate_zero_assignments_is_zero() -> None:
    assert compute_stats([], now=_NOW).pass_rate_pct == 0.0


def test_compute_stats_overdue_counts_ungraded_past_deadline_including_never_submitted() -> None:
    past = _NOW - timedelta(days=1)
    future = _NOW + timedelta(days=1)
    rows = [
        _row(assignment_id=1, grade=None, deadline=past, submission_status=None),
        _row(assignment_id=2, grade=None, deadline=future, submission_status=None),
        _row(assignment_id=3, grade=50, deadline=past),
        _row(assignment_id=4, grade=None, deadline=None),
    ]
    assert compute_stats(rows, now=_NOW).overdue_count == 1


def test_compute_stats_pending_review_counts_ungraded_non_terminal_submissions() -> None:
    rows = [
        _row(assignment_id=1, grade=None, submission_status=SubmissionStatus.TESTING),
        _row(assignment_id=2, grade=None, submission_status=SubmissionStatus.COMPLETED),
        _row(assignment_id=3, grade=None, submission_status=None),
        _row(assignment_id=4, grade=None, submission_status=SubmissionStatus.AWAITING_TEACHER_REVIEW),
    ]
    assert compute_stats(rows, now=_NOW).pending_review_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_gradebook.py -v -k compute_stats`
Expected: FAIL — `ImportError: cannot import name 'compute_stats'`

- [ ] **Step 3: Implement `compute_stats`**

Append to `src/submissions_checker/services/gradebook.py`:

```python
def compute_stats(rows: list[RosterRow], *, now: datetime) -> GradebookStats:
    graded = [r.grade for r in rows if r.grade is not None]
    average_score = round(sum(graded) / len(graded), 1) if graded else None

    by_student: dict[int, list[RosterRow]] = {}
    for r in rows:
        by_student.setdefault(r.student_id, []).append(r)
    total_students = len(by_student)
    passed_students = sum(
        1
        for student_rows in by_student.values()
        if all(r.grade is not None for r in student_rows)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_gradebook.py -v`
Expected: PASS (all tests so far)

---

### Task 4: `build_grid` — assemble the student × assignment grid

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/unit/test_gradebook.py`

**Interfaces:**
- Consumes: `RosterRow`, `GradebookGrid`/`GradebookColumn`/`GradebookRow`/`GradebookCell`, `cell_status` (Task 2)
- Produces: `build_grid(rows: list[RosterRow]) -> GradebookGrid`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_gradebook.py`:

```python
from submissions_checker.services.gradebook import build_grid


def test_build_grid_columns_follow_first_seen_row_order() -> None:
    rows = [
        _row(student_id=1, assignment_id=2, grade=None),
        _row(student_id=1, assignment_id=1, grade=None),
    ]
    grid = build_grid(rows)
    assert [c.assignment_id for c in grid.columns] == [2, 1]


def test_build_grid_row_total_sums_graded_cells_only() -> None:
    rows = [
        _row(student_id=1, assignment_id=1, grade=80),
        _row(student_id=1, assignment_id=2, grade=None),
        _row(student_id=1, assignment_id=3, grade=20),
    ]
    grid = build_grid(rows)
    assert grid.rows[0].total == 100


def test_build_grid_row_total_is_none_when_nothing_graded() -> None:
    rows = [_row(student_id=1, assignment_id=1, grade=None)]
    grid = build_grid(rows)
    assert grid.rows[0].total is None


def test_build_grid_cell_status_matches_cell_status_rule() -> None:
    rows = [_row(student_id=1, assignment_id=1, grade=90, min_grade=50)]
    grid = build_grid(rows)
    assert grid.rows[0].cells[1].status == "passed"
    assert grid.rows[0].cells[1].grade == 90


def test_build_grid_rows_sorted_by_student_name() -> None:
    rows = [
        _row(student_id=2, assignment_id=1),
        _row(student_id=1, assignment_id=1),
    ]
    grid = build_grid(rows)
    # _row() names students "Student {id}" so lexical order is Student 1, Student 2
    assert [r.student_id for r in grid.rows] == [1, 2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_gradebook.py -v -k build_grid`
Expected: FAIL — `ImportError: cannot import name 'build_grid'`

- [ ] **Step 3: Implement `build_grid`**

Append to `src/submissions_checker/services/gradebook.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_gradebook.py -v`
Expected: PASS (all tests so far)

---

### Task 5: `fetch_roster_rows` — the DB query behind stats + grid

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `RosterRow` (Task 2); reuses the "latest submission per student_assignment" subquery pattern already established at `teacher_portal.py:397-405` (`teacher_assignment` route)
- Produces: `async def fetch_roster_rows(db: AsyncSession, subject_id: int) -> list[RosterRow]`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `tests/functional/test_gradebook.py`:

```python
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
from submissions_checker.services.gradebook import fetch_roster_rows
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


# ── Arrange helpers (mirrors test_portal_detail_pages.py) ───────────────────


async def _make_subject(db: AsyncSession, owner_id: int, *, name: str = "Gradebook Subject") -> Subject:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/functional/test_gradebook.py -v -k fetch_roster_rows`
Expected: FAIL — `ImportError: cannot import name 'fetch_roster_rows'`

- [ ] **Step 3: Implement `fetch_roster_rows`**

Add these imports to the top of `src/submissions_checker/services/gradebook.py` (after the existing imports):

```python
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import (
    EntityType,
    Student,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
```

Append to `src/submissions_checker/services/gradebook.py`:

```python
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
        .outerjoin(
            latest_sub_sq, latest_sub_sq.c.students_assignment_id == StudentAssignment.id
        )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (3 tests). Needs Docker Postgres running (`make up` / `docker compose up -d`).

---

### Task 6: `fetch_integrity_rows` — quiz violations + duration-vs-median

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `IntegrityRow`, `severity_for`, `duration_anomalous`, `median_duration` (Task 2)
- Produces: `async def fetch_integrity_rows(db: AsyncSession, subject_id: int) -> list[IntegrityRow]`. Consumed by Task 7 and Task 9 (grid indicator lookup).

- [ ] **Step 1: Write the failing tests**

Append to `tests/functional/test_gradebook.py`:

```python
from submissions_checker.services.gradebook import fetch_integrity_rows


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/functional/test_gradebook.py -v -k fetch_integrity_rows`
Expected: FAIL — `ImportError: cannot import name 'fetch_integrity_rows'`

- [ ] **Step 3: Implement `fetch_integrity_rows`**

Add to the imports block in `src/submissions_checker/services/gradebook.py` (extend the existing `from submissions_checker.db.models import (...)`):

```python
from submissions_checker.db.models import (
    EntityType,
    QuizAttempt,
    Student,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
```

Append to `src/submissions_checker/services/gradebook.py`:

```python
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

    latest_by_pair: dict[tuple[int, int], object] = {}
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
        severity = severity_for(force_fail=force_fail, tab_switch=tab_switch, window_blur=window_blur)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (6 tests)

---

### Task 7: Wire the route — `teacher_subject` computes and passes gradebook + integrity data

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py:284-372`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `fetch_roster_rows`, `fetch_integrity_rows`, `compute_stats`, `build_grid` (Tasks 2-6)
- Produces: template context keys `gradebook_stats`, `gradebook_grid`, `integrity_rows`, `integrity_by_cell` (`dict[tuple[int,int], IntegrityRow]`), `default_tab` (`"overview" | "students"`). Consumed by Tasks 8-10.

- [ ] **Step 1: Write the failing test**

Append to `tests/functional/test_gradebook.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/functional/test_gradebook.py -v -k "default_tab or gradebook_context"`
Expected: FAIL — `data-default-tab` is not present in the response yet (Task 8 adds the markup this asserts on; for now these fail on the `assert 'data-default-tab=...' in resp.text` lines, and the render will also fail once you add `default_tab` to context but not yet to the template. Confirm with a failure before moving on — the first two tasks below add both halves.)

- [ ] **Step 3: Update the route**

In `src/submissions_checker/api/routes/teacher_portal.py`, add to the imports:

```python
from datetime import UTC, date, datetime
```

(This replaces the existing `from datetime import date` line at the top of the file.)

Add to the model/service imports:

```python
from submissions_checker.services.gradebook import (
    compute_stats,
    fetch_integrity_rows,
    fetch_roster_rows,
)
```

In `teacher_subject()` (`teacher_portal.py:284-372`), insert before the `return render(...)` call:

```python
    roster_rows = await fetch_roster_rows(db, subject_id)
    gradebook_stats = compute_stats(roster_rows, now=datetime.now(UTC))
    from submissions_checker.services.gradebook import build_grid

    gradebook_grid = build_grid(roster_rows)
    integrity_rows = await fetch_integrity_rows(db, subject_id)
    integrity_by_cell = {(r.student_id, r.assignment_id): r for r in integrity_rows}

    default_tab = "overview" if (enroll_result or test_student_flash) else "students"
```

(Move `build_grid` into the same top-level import as the others — the inline `from ... import build_grid` above is illustrative only; put all four names in the one import statement you just added.)

Add these keys to the `render(...)` context dict:

```python
            "gradebook_stats": gradebook_stats,
            "gradebook_grid": gradebook_grid,
            "integrity_rows": integrity_rows,
            "integrity_by_cell": integrity_by_cell,
            "default_tab": default_tab,
```

- [ ] **Step 4: Add `data-default-tab` to the template root so the test has something to assert on**

In `templates/teacher_subject.html`, change the opening line of the content block:

```html
{% block content %}
```

to:

```html
{% block content %}
<div data-default-tab="{{ default_tab }}">
```

and add the matching closing `</div>` at the very end of the file, immediately before `{% endblock %}`. (Task 8 replaces this placeholder wrapper with the real tab markup; this step exists only so Task 7's test is meaningful on its own.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (9 tests). Also run `pytest tests/functional/test_teacher_portal.py tests/functional/test_portal_detail_pages.py -v` to confirm nothing existing broke — expected: all still PASS (the new context keys are additive).

---

### Task 8: Tab navigation — restructure the template into four panels

**Files:**
- Modify: `templates/teacher_subject.html` (full restructure)
- Modify: `i18n/uk.yml` (insert before line 491, i.e. before the `admin:` section)
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `default_tab` (Task 7)
- Produces: four `<div id="tab-panel-{overview,students,assignments,grades}">` panels + `<button data-tab-target="...">` triggers; a `<script>` block wiring click-to-show. Task 9 and 10 add content inside `tab-panel-grades`.

- [ ] **Step 1: Write the failing test**

Append to `tests/functional/test_gradebook.py`:

```python
async def test_teacher_subject_has_four_tabs(client: AsyncClient, db: AsyncSession, teacher) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    for target in ["overview", "students", "assignments", "grades"]:
        assert f'data-tab-target="{target}"' in resp.text
        assert f'id="tab-panel-{target}"' in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/functional/test_gradebook.py -v -k has_four_tabs`
Expected: FAIL — no `data-tab-target` attributes exist yet

- [ ] **Step 3: Add vocab keys**

In `i18n/uk.yml`, insert immediately before the blank line that precedes `admin:` (currently line 490-491, right after `no_quiz_settings: ...`):

```yaml
  tab_overview: Огляд
  tab_students: Студенти
  tab_assignments: Завдання
  tab_grades: Оцінки
```

- [ ] **Step 4: Restructure the template**

Rewrite `templates/teacher_subject.html`'s content block. Keep every existing block's *inner content* byte-for-byte (enroll card, students list, assignments list, test-student panel) — only their wrapping and position change. The new structure:

```html
{% block content %}
<div data-default-tab="{{ default_tab }}">

<div class="flex items-start justify-between mb-6">
  <div>
    <h1 class="text-2xl font-bold text-slate-900">{{ subject.name }}</h1>
    {% if subject.description %}
    <p class="text-slate-500 text-sm mt-1">{{ subject.description }}</p>
    {% endif %}
  </div>
  <div class="flex gap-2 shrink-0 flex-wrap">
    <a href="/teacher/subjects/{{ subject.id }}/feedback"
       class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-indigo-200 bg-indigo-50 text-indigo-700 text-sm hover:bg-indigo-100 transition-colors">
      {{ vocab.teacher.view_feedback }}
    </a>
    {% if not current_semester %}
    <span title="{{ vocab.teacher.no_active_semester_hint }}" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-400 text-sm cursor-not-allowed">
      {{ vocab.teacher.request_feedback }}
    </span>
    {% elif feedback_request %}
    <span title="{{ vocab.teacher.feedback_already_sent }}" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-400 text-sm cursor-not-allowed">
      {{ vocab.teacher.feedback_sent_badge }}
    </span>
    {% else %}
    <form method="POST" action="/teacher/subjects/{{ subject.id }}/feedback/request">
      <button type="submit"
              class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-indigo-300 bg-indigo-600 text-white text-sm hover:bg-indigo-700 transition-colors">
        {{ vocab.teacher.request_feedback }}
      </button>
    </form>
    {% endif %}
  </div>
</div>

{% if feedback_sent %}
<div class="mb-4 px-4 py-3 rounded-lg bg-green-50 border border-green-200 text-green-800 text-sm">
  {{ vocab.teacher.feedback_sent_msg }}
</div>
{% elif feedback_error == "already_sent" %}
<div class="mb-4 px-4 py-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-sm">
  {{ vocab.teacher.feedback_already_sent }}
</div>
{% elif feedback_error == "no_active_semester" %}
<div class="mb-4 px-4 py-3 rounded-lg bg-slate-50 border border-slate-200 text-slate-600 text-sm">
  {{ vocab.teacher.feedback_no_active_semester }}
</div>
{% endif %}

<div class="flex gap-1 border-b border-slate-200 mb-6" role="tablist">
  <button type="button" data-tab-target="overview" role="tab"
          class="tab-trigger px-4 py-2.5 text-sm font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-900">
    {{ vocab.teacher.tab_overview }}
  </button>
  <button type="button" data-tab-target="students" role="tab"
          class="tab-trigger px-4 py-2.5 text-sm font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-900">
    {{ vocab.teacher.tab_students }}
  </button>
  <button type="button" data-tab-target="assignments" role="tab"
          class="tab-trigger px-4 py-2.5 text-sm font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-900">
    {{ vocab.teacher.tab_assignments }}
  </button>
  <button type="button" data-tab-target="grades" role="tab"
          class="tab-trigger px-4 py-2.5 text-sm font-medium border-b-2 border-transparent text-slate-500 hover:text-slate-900">
    {{ vocab.teacher.tab_grades }}
  </button>
</div>

<div id="tab-panel-overview" class="tab-panel" hidden>
  {% if enroll_result %}
  <div class="mb-4 px-4 py-3 rounded-lg border text-sm {% if enroll_result.rejected %}bg-amber-50 border-amber-200 text-amber-800{% else %}bg-emerald-50 border-emerald-200 text-emerald-800{% endif %}">
    <p>
      {{ vocab.teacher.enroll_result_enrolled }}: <strong>{{ enroll_result.enrolled }}</strong> ·
      {{ vocab.teacher.enroll_result_already }}: <strong>{{ enroll_result.already }}</strong> ·
      {{ vocab.teacher.enroll_result_rejected }}: <strong>{{ enroll_result.rejected }}</strong>
    </p>
    {% if enroll_result.rejected_rows %}
    <p class="mt-2 font-medium">{{ vocab.teacher.enroll_rejected_title }}</p>
    <ul class="mt-1 space-y-0.5">
      {% for r in enroll_result.rejected_rows %}
      <li>
        {{ vocab.teacher.enroll_row_label }} {{ r.line }} —
        {% if r.reason == "unknown" %}{{ vocab.teacher.enroll_reason_unknown }}
        {% else %}{{ vocab.teacher.enroll_reason_empty }}{% endif %}
      </li>
      {% endfor %}
    </ul>
    {% if enroll_result.rejected_overflow %}
    <p class="mt-1">{{ vocab.teacher.enroll_rejected_overflow }} {{ enroll_result.rejected_overflow }}</p>
    {% endif %}
    {% endif %}
  </div>
  {% endif %}

  {% if test_student_flash == "created" %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-teal-50 border border-teal-200 text-teal-800 text-sm">
    Test student created. Credentials are shown in the panel below.
  </div>
  {% elif test_student_flash == "existing" %}
  <div class="mb-4 px-4 py-3 rounded-lg bg-slate-50 border border-slate-200 text-slate-600 text-sm">
    A test student already exists for this subject.
  </div>
  {% endif %}

  <div class="bg-white rounded-xl border border-slate-200 shadow-sm p-5 mb-6">
    <h2 class="font-semibold text-slate-900 mb-1">{{ vocab.teacher.enroll_title }}</h2>
    <p class="text-sm text-slate-500 mb-4">{{ vocab.teacher.enroll_hint }}</p>
    <div class="flex flex-col sm:flex-row gap-4 items-start">
      <a href="/teacher/subjects/{{ subject.id }}/students/template.csv"
         class="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-slate-300 bg-slate-50 text-sm font-medium text-slate-700 hover:bg-slate-100 transition-colors shrink-0">
        <svg class="w-4 h-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
        </svg>
        {{ vocab.teacher.enroll_download_template }}
      </a>
      <form action="/teacher/subjects/{{ subject.id }}/students/import" method="POST"
            enctype="multipart/form-data" class="flex flex-col sm:flex-row gap-3 items-start w-full">
        <div class="flex flex-col gap-1 w-full sm:max-w-xs">
          <label class="sr-only" for="enroll-csv">CSV</label>
          <input id="enroll-csv" type="file" name="file" accept=".csv,text/csv" required
                 class="block w-full text-sm text-slate-600 file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100 border border-slate-300 rounded-lg cursor-pointer" />
          <p class="text-xs text-slate-400">{{ vocab.teacher.enroll_columns }} <code class="bg-slate-100 px-1 rounded">email, variant</code></p>
        </div>
        <button type="submit"
                class="shrink-0 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 transition-colors">
          {{ vocab.teacher.enroll_submit }}
        </button>
      </form>
    </div>
  </div>

  {% if current_user.user_id == subject.owner_id %}
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
      <svg class="w-4 h-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21a48.25 48.25 0 0 1-8.135-.687c-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" />
      </svg>
      <h2 class="font-semibold text-slate-900">Test Student</h2>
      <span class="ml-auto text-xs text-slate-400 font-medium">Owner only</span>
    </div>

    <div class="px-5 py-4">
      {% if test_student_info %}
      <p class="text-sm text-slate-500 mb-4">Use these credentials to log in as the test student and verify your assignment setup. Stats and enrollment counts exclude this account.</p>
      <div class="flex flex-col sm:flex-row gap-3 mb-4">
        <div class="flex-1 bg-slate-50 rounded-lg border border-slate-200 px-4 py-3">
          <p class="text-xs text-slate-400 mb-1">Username</p>
          <p class="text-sm font-mono font-medium text-slate-800 select-all">{{ test_student_info.username }}</p>
        </div>
        <div class="flex-1 bg-slate-50 rounded-lg border border-slate-200 px-4 py-3">
          <p class="text-xs text-slate-400 mb-1">Password</p>
          <p class="text-sm font-mono font-medium text-slate-800 select-all">{{ test_student_info.plain_password }}</p>
        </div>
      </div>
      <form method="POST" action="/teacher/subjects/{{ subject.id }}/test-student/enter"
            onsubmit="return confirm('You will be switched to the test student session. Log out and back in to return to your teacher account.')">
        <button type="submit"
                class="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-teal-600 text-white text-sm font-medium hover:bg-teal-700 transition-colors">
          Enter as Test Student
        </button>
      </form>
      {% else %}
      <p class="text-sm text-slate-500 mb-4">Create a test student to verify that your assignments and grading setup work correctly before real students enroll.</p>
      <form method="POST" action="/teacher/subjects/{{ subject.id }}/test-student">
        {% for a in assignments %}
          {% set variants = (a.config or {}).get('variants') or {} %}
          {% if variants %}
          <div class="mb-3">
            <label class="block text-xs font-medium text-slate-500 mb-1">{{ a.title }} — variant</label>
            <select name="variant_{{ a.code }}" class="text-sm border border-slate-300 rounded-lg px-3 py-1.5">
              {% if not a.config.get('variants_required') %}
              <option value="">No variant</option>
              {% endif %}
              {% for v in variants.keys() | sort %}
              <option value="{{ v }}">{{ v }}</option>
              {% endfor %}
            </select>
          </div>
          {% endif %}
        {% endfor %}
        <button type="submit"
                class="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border border-teal-300 bg-teal-50 text-teal-700 text-sm font-medium hover:bg-teal-100 transition-colors">
          Create Test Student
        </button>
      </form>
      {% endif %}
    </div>
  </div>
  {% endif %}
</div>

<div id="tab-panel-students" class="tab-panel" hidden>
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
      <h2 class="font-semibold text-slate-900">{{ vocab.teacher.enrolled_students }}</h2>
      <span class="text-xs text-slate-400 font-medium">{{ students | length }}</span>
    </div>

    {% if students %}
    <ul class="divide-y divide-slate-100">
      {% for item in students %}
      <li class="px-5 py-3.5">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-full bg-indigo-100 text-indigo-700 font-semibold text-sm flex items-center justify-center shrink-0">
            {{ item.student.full_name[0] }}
          </div>
          <div class="min-w-0">
            <p class="text-sm font-medium text-slate-900 truncate">{{ item.student.full_name }}</p>
            <p class="text-xs text-slate-400 truncate">{{ item.group_name or "?" }} · @{{ item.student.github_username }}</p>
          </div>
        </div>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <div class="px-5 py-8 text-center text-slate-400 text-sm">{{ vocab.teacher.no_students_enrolled }}</div>
    {% endif %}
  </div>
</div>

<div id="tab-panel-assignments" class="tab-panel" hidden>
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
      <h2 class="font-semibold text-slate-900">{{ vocab.teacher.assignments }}</h2>
    </div>

    {% if assignments %}
    <ul class="divide-y divide-slate-100">
      {% for a in assignments %}
      <li>
        <a href="/teacher/subjects/{{ subject.id }}/assignments/{{ a.id }}"
           class="flex items-center gap-3 px-5 py-3.5 hover:bg-slate-50 transition-colors group">
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium text-slate-900 group-hover:text-indigo-700 transition-colors truncate">{{ a.title }}</p>
            <p class="text-xs text-slate-400 mt-0.5">
              {% if a.deadline %}
                {{ vocab.teacher.deadline_label }} {{ a.deadline.strftime("%b %d, %Y") }}
              {% else %}
                {{ vocab.teacher.no_deadline }}
              {% endif %}
              &nbsp;·&nbsp; {{ a.min_grade }}–{{ a.max_grade }} pts
            </p>
          </div>
          <svg class="w-4 h-4 text-slate-300 group-hover:text-indigo-400 transition-colors shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
          </svg>
        </a>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <div class="px-5 py-8 text-center text-slate-400 text-sm">{{ vocab.teacher.no_assignments_yet }}</div>
    {% endif %}
  </div>
</div>

<div id="tab-panel-grades" class="tab-panel" hidden>
  <!-- Task 9 and Task 10 fill this in -->
</div>

</div>

<script>
(function () {
  var root = document.currentScript.previousElementSibling.parentElement;
  var triggers = root.querySelectorAll(".tab-trigger");
  var panels = root.querySelectorAll(".tab-panel");

  function activate(name) {
    triggers.forEach(function (btn) {
      var isActive = btn.dataset.tabTarget === name;
      btn.classList.toggle("border-indigo-600", isActive);
      btn.classList.toggle("text-indigo-700", isActive);
      btn.classList.toggle("text-slate-500", !isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.id !== "tab-panel-" + name;
    });
  }

  triggers.forEach(function (btn) {
    btn.addEventListener("click", function () {
      activate(btn.dataset.tabTarget);
    });
  });

  activate(root.dataset.defaultTab || "students");
})();
</script>
{% endblock %}
```

Note this replaces the placeholder `<div data-default-tab="...">...</div>` wrapper added in Task 7 Step 4 with the real content — the wrapper `<div>` now contains the header, tab bar, and four panels, and the closing `</div>` sits right before the `<script>` block.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (10 tests)

- [ ] **Step 6: Run the full existing portal test suites to confirm nothing broke**

Run: `pytest tests/functional/test_teacher_portal.py tests/functional/test_portal_detail_pages.py -v`
Expected: PASS — all existing assertions are substring-in-`resp.text` checks (e.g. `"Vio Lator" in resp.text`), which still hold since moved content is still rendered, just wrapped in a `hidden` panel.

---

### Task 9: Gradebook stat cards + grid markup

**Files:**
- Modify: `templates/teacher_subject.html` (`tab-panel-grades`)
- Modify: `i18n/uk.yml`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `gradebook_stats` (`GradebookStats`), `gradebook_grid` (`GradebookGrid` with `.columns: list[GradebookColumn]`, `.rows: list[GradebookRow]` where each `GradebookRow.cells` is `dict[int, GradebookCell]` keyed by `assignment_id`) — Task 7
- Produces: rendered stat cards + grid inside `tab-panel-grades`. Task 10 adds the integrity table below this in the same panel.

- [ ] **Step 1: Write the failing test**

Append to `tests/functional/test_gradebook.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/functional/test_gradebook.py -v -k grades_tab_renders`
Expected: FAIL — grid markup doesn't exist yet, so neither the assignment title nor the grade value appear in `tab-panel-grades`

- [ ] **Step 3: Add vocab keys**

In `i18n/uk.yml`, extend the block added in Task 8 (still before `admin:`):

```yaml
  gb_stat_avg_score: Середній бал
  gb_stat_pass_rate: "% зданих"
  gb_stat_overdue: Прострочені
  gb_stat_pending: На перевірці
  gb_grid_title: Відомість оцінок
  gb_col_total: Разом
  gb_no_data: Немає даних для відображення.
```

- [ ] **Step 4: Fill in `tab-panel-grades`**

Replace the `<!-- Task 9 and Task 10 fill this in -->` placeholder in `templates/teacher_subject.html` with:

```html
<div id="tab-panel-grades" class="tab-panel" hidden>
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    <div class="bg-white rounded-lg border border-slate-200 px-4 py-3">
      <div class="text-xl font-bold text-slate-900">
        {{ gradebook_stats.average_score if gradebook_stats.average_score is not none else "—" }}
      </div>
      <div class="text-xs text-slate-400 mt-0.5">{{ vocab.teacher.gb_stat_avg_score }}</div>
    </div>
    <div class="bg-white rounded-lg border border-slate-200 px-4 py-3">
      <div class="text-xl font-bold text-green-600">{{ gradebook_stats.pass_rate_pct }}%</div>
      <div class="text-xs text-slate-400 mt-0.5">{{ vocab.teacher.gb_stat_pass_rate }}</div>
    </div>
    <div class="bg-white rounded-lg border border-slate-200 px-4 py-3">
      <div class="text-xl font-bold text-red-600">{{ gradebook_stats.overdue_count }}</div>
      <div class="text-xs text-slate-400 mt-0.5">{{ vocab.teacher.gb_stat_overdue }}</div>
    </div>
    <div class="bg-white rounded-lg border border-slate-200 px-4 py-3">
      <div class="text-xl font-bold text-amber-600">{{ gradebook_stats.pending_review_count }}</div>
      <div class="text-xs text-slate-400 mt-0.5">{{ vocab.teacher.gb_stat_pending }}</div>
    </div>
  </div>

  {% if gradebook_grid.rows %}
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm">
    <div class="px-5 py-4 border-b border-slate-100">
      <h2 class="font-semibold text-slate-900">{{ vocab.teacher.gb_grid_title }}</h2>
    </div>
    <div class="overflow-x-auto">
      <table class="text-sm border-collapse">
        <thead>
          <tr class="bg-slate-50">
            <th class="sticky left-0 z-10 bg-slate-50 px-4 py-2 text-left font-medium text-slate-600 border-b border-slate-200 min-w-[180px]">
              {{ vocab.teacher.enrolled_students }}
            </th>
            {% for col in gradebook_grid.columns %}
            <th class="px-4 py-2 text-center font-medium text-slate-600 border-b border-slate-200 whitespace-nowrap">
              {{ col.title }}
            </th>
            {% endfor %}
            <th class="sticky right-0 z-10 bg-slate-50 px-4 py-2 text-center font-medium text-slate-600 border-b border-slate-200">
              {{ vocab.teacher.gb_col_total }}
            </th>
          </tr>
        </thead>
        <tbody>
          {% for row in gradebook_grid.rows %}
          <tr class="border-b border-slate-100">
            <td class="sticky left-0 z-10 bg-white px-4 py-2 font-medium text-slate-900 whitespace-nowrap">
              {{ row.student_name }}
            </td>
            {% for col in gradebook_grid.columns %}
            {% set cell = row.cells.get(col.assignment_id) %}
            {% set violation = integrity_by_cell.get((row.student_id, col.assignment_id)) %}
            <td id="cell-{{ row.student_id }}-{{ col.assignment_id }}"
                class="px-4 py-2 text-center relative
                  {% if cell and cell.status == 'passed' %}bg-green-50 text-green-700
                  {% elif cell and cell.status == 'failed' %}bg-red-50 text-red-700
                  {% elif cell and cell.status == 'pending' %}bg-amber-50 text-amber-700
                  {% else %}text-slate-300{% endif %}">
              {% if cell and cell.grade is not none %}{{ cell.grade }}{% else %}—{% endif %}
              {% if violation %}
              <span title="tab_switch: {{ violation.tab_switch }}, window_blur: {{ violation.window_blur }}{% if violation.force_fail %}, force_fail{% endif %}"
                    class="violation-dot absolute top-0.5 right-0.5 w-1.5 h-1.5 rounded-full bg-red-500 cursor-pointer"
                    data-scroll-to="integrity-row-{{ row.student_id }}-{{ col.assignment_id }}"></span>
              {% endif %}
            </td>
            {% endfor %}
            <td class="sticky right-0 z-10 bg-white px-4 py-2 text-center font-semibold text-slate-900">
              {{ row.total if row.total is not none else "—" }}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm px-5 py-8 text-center text-slate-400 text-sm">
    {{ vocab.teacher.gb_no_data }}
  </div>
  {% endif %}
</div>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (11 tests)

---

### Task 10: Integrity table + grid violation-dot click-to-scroll

**Files:**
- Modify: `templates/teacher_subject.html` (`tab-panel-grades`, append below the grid; extend the tab script)
- Modify: `i18n/uk.yml`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `integrity_rows: list[IntegrityRow]` (Task 7); `violation-dot[data-scroll-to]` elements from Task 9
- Produces: full feature — nothing downstream depends on this task's output.

- [ ] **Step 1: Write the failing test**

Append to `tests/functional/test_gradebook.py`:

```python
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
        db, sub.id, started_at=start, submitted_at=start + timedelta(seconds=100),
        violations={"tab_switch": 3},
    )

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}")

    assert resp.status_code == 200
    assert "Flagged Student" in resp.text
    assert f'id="integrity-row-{student.id}-{a1.id}"' in resp.text
    assert f'id="cell-{student.id}-{a1.id}"' in resp.text
    assert "violation-dot" in resp.text
    assert 'data-flagged-only-toggle' in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/functional/test_gradebook.py -v -k integrity_table_shows`
Expected: FAIL — no integrity table markup exists yet

- [ ] **Step 3: Add vocab keys**

In `i18n/uk.yml`, extend the same block:

```yaml
  integrity_title: Доброчесність тестів
  integrity_col_student: Студент
  integrity_col_assignment: Завдання
  integrity_col_tab_switch: Перемикання вкладок
  integrity_col_window_blur: Втрата фокусу вікна
  integrity_col_duration: Тривалість
  integrity_col_severity: Рівень
  integrity_severity_high: Високий
  integrity_severity_medium: Середній
  integrity_flagged_only: Показати лише позначені
  integrity_no_data: Немає спроб тестів для цього предмета.
  integrity_median_suffix: медіана
```

- [ ] **Step 4: Append the integrity table**

In `templates/teacher_subject.html`, inside `tab-panel-grades`, add this immediately after the grid's closing `{% endif %}` (the one that closes the `{% if gradebook_grid.rows %}...{% else %}...{% endif %}` block from Task 9), still inside `<div id="tab-panel-grades">`:

```html
  <div class="bg-white rounded-xl border border-slate-200 shadow-sm mt-6">
    <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between flex-wrap gap-3">
      <h2 class="font-semibold text-slate-900">{{ vocab.teacher.integrity_title }}</h2>
      <label class="inline-flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" data-flagged-only-toggle class="rounded border-slate-300">
        {{ vocab.teacher.integrity_flagged_only }}
      </label>
    </div>
    {% if integrity_rows %}
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="bg-slate-50 border-b border-slate-200">
            <th class="px-4 py-2 text-left font-medium text-slate-600">{{ vocab.teacher.integrity_col_student }}</th>
            <th class="px-4 py-2 text-left font-medium text-slate-600">{{ vocab.teacher.integrity_col_assignment }}</th>
            <th class="px-4 py-2 text-center font-medium text-slate-600">{{ vocab.teacher.integrity_col_tab_switch }}</th>
            <th class="px-4 py-2 text-center font-medium text-slate-600">{{ vocab.teacher.integrity_col_window_blur }}</th>
            <th class="px-4 py-2 text-center font-medium text-slate-600">{{ vocab.teacher.integrity_col_duration }}</th>
            <th class="px-4 py-2 text-center font-medium text-slate-600">{{ vocab.teacher.integrity_col_severity }}</th>
          </tr>
        </thead>
        <tbody>
          {% for row in integrity_rows %}
          <tr id="integrity-row-{{ row.student_id }}-{{ row.assignment_id }}"
              data-flagged="{{ 'true' if row.flagged else 'false' }}"
              class="border-b border-slate-100">
            <td class="px-4 py-2 text-slate-900">{{ row.student_name }}</td>
            <td class="px-4 py-2 text-slate-600">{{ row.assignment_title }}</td>
            <td class="px-4 py-2 text-center">{{ row.tab_switch }}</td>
            <td class="px-4 py-2 text-center">{{ row.window_blur }}</td>
            <td class="px-4 py-2 text-center {% if row.duration_anomalous %}text-red-600 font-medium{% else %}text-slate-600{% endif %}">
              {% if row.duration_seconds is not none %}
                {{ row.duration_seconds }}s
                {% if row.median_seconds is not none %}/ {{ row.median_seconds }}s {{ vocab.teacher.integrity_median_suffix }}{% endif %}
              {% else %}—{% endif %}
            </td>
            <td class="px-4 py-2 text-center">
              {% if row.severity == "high" %}
              <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-700">{{ vocab.teacher.integrity_severity_high }}</span>
              {% elif row.severity == "medium" %}
              <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700">{{ vocab.teacher.integrity_severity_medium }}</span>
              {% else %}
              <span class="text-slate-300">—</span>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div class="px-5 py-8 text-center text-slate-400 text-sm">{{ vocab.teacher.integrity_no_data }}</div>
    {% endif %}
  </div>
```

- [ ] **Step 5: Wire the filter checkbox and the violation-dot click-to-scroll**

In `templates/teacher_subject.html`, extend the `<script>` block added in Task 8 — add this inside the same IIFE, after the existing `activate(root.dataset.defaultTab || "students");` line:

```javascript
  var flaggedToggle = root.querySelector("[data-flagged-only-toggle]");
  if (flaggedToggle) {
    flaggedToggle.addEventListener("change", function () {
      root.querySelectorAll("tr[data-flagged]").forEach(function (tr) {
        tr.hidden = flaggedToggle.checked && tr.dataset.flagged !== "true";
      });
    });
  }

  root.querySelectorAll(".violation-dot").forEach(function (dot) {
    dot.addEventListener("click", function (event) {
      event.stopPropagation();
      activate("grades");
      var target = document.getElementById(dot.dataset.scrollTo);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("bg-yellow-50");
        setTimeout(function () { target.classList.remove("bg-yellow-50"); }, 1500);
      }
    });
  });
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/functional/test_gradebook.py -v`
Expected: PASS (12 tests)

---

### Task 11: Full verification, lint, and local review handoff

**Files:** None modified — verification only.

**Interfaces:** None.

- [ ] **Step 1: Run the full test suite**

Run: `pytest --cov=submissions_checker --cov-report=term-missing`
Expected: all tests PASS, including every pre-existing suite (`tests/unit`, `tests/functional`, `tests/integration`) — no regressions from the template restructure or the new route context keys.

- [ ] **Step 2: Run lint and type checks**

Run: `ruff check src/ tests/`
Expected: no new findings.

Run: `mypy src/` (or whatever the project's configured mypy invocation is — check `Makefile`/`pyproject.toml` if a dedicated target exists)
Expected: no new findings in `services/gradebook.py` or `api/routes/teacher_portal.py`.

- [ ] **Step 3: Manual browser check**

With `docker compose up -d` running, log in as `teacher` / `teacher123`, open a subject the `teacher` user owns (e.g. `/teacher/subjects/3` against the seeded dev data), and confirm: all four tabs switch correctly; Огляд auto-opens after a CSV enroll or test-student action; the Оцінки tab shows the four stat cards, the grid with sticky name/Разом columns and horizontal scroll, and the integrity table below it; the "show flagged only" checkbox filters rows; clicking a violation dot jumps to and briefly highlights the matching integrity row. Since the currently owned seeded subject (id 3) has no deadlines or quiz attempts, either seed one manually or temporarily add a deadline + a `QuizAttempt` with violations via `docker compose exec postgres psql` to see the grid's failed/overdue coloring and the integrity table populated.

- [ ] **Step 4: Stop and hand back**

Per the Global Constraints: do not commit. Report to the user what was implemented and verified (Steps 1-3 above), and wait for them to review locally and give the go-ahead before any `git add`/`git commit` happens.

---

## Self-Review

**Spec coverage:**
- Bug fix (`?` fallback) → Task 1.
- Tabs, default-tab logic, Test Student panel moved to Огляд → Tasks 7-8.
- 3a stat cards (all four numbers, server-computed) → Tasks 2-3, 9.
- 3b read-only grid (sticky columns, horizontal scroll, color-coded cells, Разом column, no inline editing) → Tasks 2, 4-5, 9.
- Section 4 integrity table (all six columns, severity rule, duration-vs-median flag, flagged-only filter) → Tasks 2, 6, 10.
- Grid cell violation indicator (hover tooltip + click-to-detail) → Tasks 9-10.
- Non-goals (no CSV export, no editing, feedback button untouched) → never introduced anywhere in this plan; feedback button markup is carried through Task 8 unchanged.

**Placeholder scan:** none — every step has literal code, exact file paths/line numbers, and concrete `pytest -k` invocations with expected outcomes.

**Type consistency:** `RosterRow`, `GradebookStats`, `GradebookCell`, `GradebookColumn`, `GradebookRow`, `GradebookGrid`, and `IntegrityRow` are defined once in Task 2 and referenced with the same field names throughout Tasks 3-10 (`grid.columns`, `grid.rows[i].cells[assignment_id]`, `row.flagged`, `row.duration_seconds`, etc.) — checked against each task's actual usage while writing this plan.
