# Subject Tabs Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the just-shipped Оцінки tab and Огляд tab with a reworked four-tab layout — Панель (scheduled-job-backed stat cards) → Завдання (+ per-task pending count) → Студенти (new quiz/review sub-mark grid) → Операції (renamed Огляд, moved last, + search-by-email enroll + relocated feedback button).

**Architecture:** A new small cache table + APScheduler job (same pattern as the existing `metrics_refresh`/`teacher_digest_processor` jobs) precomputes four subject-wide numbers every ~5 minutes; the route reads the cached row instead of computing live. A new DB query (`fetch_grid_rows`) feeds a richer per-student grid reading `Submission.grade_breakdown`'s `quiz_score`/`quality_score`. Two new routes add search-by-email enrollment, reusing the existing `_ensure_assignment_rows` enrollment helper. This continues directly on top of the just-completed gradebook/integrity branch — `fetch_roster_rows`, `fetch_integrity_rows`, `cell_status`, `severity_for`, `duration_anomalous`, `median_duration` all survive unchanged and get a second caller (the new scheduled job); `compute_stats`/`build_grid`/`GradebookStats`/`GradebookGrid`/`GradebookRow`/`GradebookColumn`/`GradebookCell` are fully superseded and deleted in the cleanup task once nothing references them.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, PostgreSQL (JSONB), Alembic, APScheduler, Jinja2, Tailwind, vanilla JS. Tests: pytest-asyncio; `tests/functional` (transactional DB per test, HTTP-level) for routes/templates; `tests/integration` (real Postgres via testcontainers, direct function calls) for the scheduled job, matching how `teacher_digest_processor`/`outbox_processor` are already tested there.

**Spec:** `docs/superpowers/specs/2026-09-16-subject-tabs-rework-design.md` (supersedes the stat-card/grid/integrity-table portions of `docs/superpowers/specs/2026-09-16-gradebook-integrity-tab-design.md`, which the already-shipped branch implemented)

## Global Constraints

- No CSV export anywhere in this feature (unchanged from the prior spec).
- No inline editing of marks anywhere.
- Task-level pages (`teacher_assignment.html` and its route) are untouched.
- **Середній бал** = `100 * Σgrade / Σmax_grade` over graded pairs only (ungraded pairs excluded from both sums, not counted as zero); `null` when nothing is graded yet.
- **% зданих** = passed pairs / **all** enrolled×assignment pairs (ungraded counts as not-passed) — per-work, not per-student.
- **% списувань** = flagged attempts / all finalized quiz attempts, subject-wide; `null` when there are zero finalized attempts.
- Stat cards are computed by a scheduled job on a ~5 minute cadence, not live per request. A brand-new subject with no job run yet shows a placeholder, not a crash or a misleading 0.
- The per-attempt integrity table and the grid's violation-dot indicator are removed entirely.
- Reuse `_ensure_assignment_rows` for the new search-enroll flow's DB writes — enrollment semantics must be identical across the button-enroll, CSV, and search-enroll paths.
- Work happens in the isolated worktree on branch `worktree-gradebook-integrity-tab` (already set up, already has the prior branch's commits). **Commit normally per task** (per this session's established ruling — the worktree boundary is what protects `main`, not withholding commits). Never merge/push without the user's explicit go-ahead at the end.

---

## File Structure

| File | Responsibility |
|---|---|
| `alembic/versions/0026_subject_gradebook_stats.py` | New migration: `subject_gradebook_stats` table |
| `src/submissions_checker/db/models/subject_gradebook_stats.py` | New model |
| `src/submissions_checker/db/models/__init__.py` | Export the new model |
| `src/submissions_checker/services/gradebook.py` | New: `CachedSubjectStats`, `compute_cached_stats`, `GridSourceRow`/`GridCell`/`GridColumn`/`GridRow`/`StudentGrid`, `fetch_grid_rows`, `build_student_grid`. Deleted (cleanup task): `GradebookStats`, `compute_stats`, `GradebookGrid`/`GradebookRow`/`GradebookColumn`/`GradebookCell`, `build_grid` |
| `src/submissions_checker/workers/scheduled/subject_stats_refresh.py` | New scheduled job |
| `src/submissions_checker/core/scheduler.py` | Register the new job |
| `src/submissions_checker/core/config.py` | New `subject_stats_refresh_interval` setting |
| `src/submissions_checker/api/routes/teacher_portal.py` | `teacher_subject()` rewired to the new data; two new routes (search, enroll-by-search) |
| `templates/teacher_subject.html` | Tab rename/reorder, Панель/Завдання/Студенти/Операції content, `default_tab` wiring |
| `i18n/uk.yml` | New/removed vocab keys |
| `tests/unit/test_gradebook.py` | New tests for `compute_cached_stats`, `build_student_grid` |
| `tests/functional/test_gradebook.py` | New/updated tests for `fetch_grid_rows`, the two new routes, the reworked `teacher_subject` render |
| `tests/integration/test_subject_stats_refresh.py` | New: direct-call test for the scheduled job, matching `tests/integration/test_teacher_digest.py`'s pattern |

---

### Task 1: Migration + `SubjectGradebookStats` model

**Files:**
- Create: `alembic/versions/0026_subject_gradebook_stats.py`
- Create: `src/submissions_checker/db/models/subject_gradebook_stats.py`
- Modify: `src/submissions_checker/db/models/__init__.py`
- Test: `tests/unit/test_gradebook.py` (a lightweight smoke import/attrs test), plus this model's shape is exercised for real by Task 3's functional tests and Task 4's integration test

**Interfaces:**
- Produces: `SubjectGradebookStats` ORM model with columns `subject_id` (PK, FK→`subjects.id` `ondelete="CASCADE"`), `pending_review_count` (int, not null), `average_mark_pct` (float, nullable), `pass_pct` (float, not null), `cheating_pct` (float, nullable), `computed_at` (timestamptz, not null). Consumed by Task 4 (job writes it) and Task 6 (route reads it).

- [ ] **Step 1: Write the model**

Look at `src/submissions_checker/db/models/teacher_notification_queue.py` first for the exact import/style convention this repo uses for a small model file (plain `Base`, no `TimestampMixin` needed here since we have our own `computed_at`).

Create `src/submissions_checker/db/models/subject_gradebook_stats.py`:

```python
"""Cached, scheduled-job-computed subject-wide gradebook stats.

Written by workers.scheduled.subject_stats_refresh on a ~5 minute interval
(subject_stats_refresh_interval setting) — read live by the teacher_subject
route instead of being computed per request. See gradebook.compute_cached_stats
for the pure calculation this table's values come from.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base


class SubjectGradebookStats(Base):
    __tablename__ = "subject_gradebook_stats"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True
    )
    pending_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    average_mark_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pass_pct: Mapped[float] = mapped_column(Float, nullable=False)
    cheating_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Write the migration**

Look at `alembic/versions/0021_teacher_notifications.py` for this repo's exact `op.create_table(...)` style for a brand-new table (NOT raw `op.execute` — that style is only used for ALTER-only migrations elsewhere in this repo).

Create `alembic/versions/0026_subject_gradebook_stats.py`:

```python
"""Add subject_gradebook_stats cache table.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subject_gradebook_stats",
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("pending_review_count", sa.Integer(), nullable=False),
        sa.Column("average_mark_pct", sa.Float(), nullable=True),
        sa.Column("pass_pct", sa.Float(), nullable=False),
        sa.Column("cheating_pct", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subject_id"),
    )


def downgrade() -> None:
    op.drop_table("subject_gradebook_stats")
```

Confirm `0025` is still the current head (check `alembic/versions/` for anything newer before writing `down_revision`).

- [ ] **Step 3: Export the model**

In `src/submissions_checker/db/models/__init__.py`, add the import and `__all__` entry, alongside the other model imports (alphabetical among the `subject_*` group):

```python
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
```

and add `"SubjectGradebookStats"` to `__all__` in the same alphabetical position as the other `Subject*` names.

- [ ] **Step 4: Run the migration against the dev DB and verify**

Run: `docker compose exec app alembic upgrade head` (or `uv run alembic upgrade head` if running migrations outside Docker against a reachable Postgres — use whichever this repo's `Makefile`/README documents for local migration runs)
Expected: migration `0026` applies cleanly, `\d subject_gradebook_stats` in `psql` shows the five columns plus the FK.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0026_subject_gradebook_stats.py src/submissions_checker/db/models/subject_gradebook_stats.py src/submissions_checker/db/models/__init__.py
git commit -m "Add subject_gradebook_stats cache table for scheduled-job stats"
```

---

### Task 2: `compute_cached_stats` — pure calculation for the four stat cards

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/unit/test_gradebook.py`

**Interfaces:**
- Consumes: `RosterRow` (existing), `IntegrityRow` (existing) — both already defined in `gradebook.py` from the prior branch
- Produces: `CachedSubjectStats` dataclass, `compute_cached_stats(roster_rows: list[RosterRow], integrity_rows: list[IntegrityRow], *, now: datetime) -> CachedSubjectStats`. Consumed by Task 4 (the scheduled job).

This task is purely additive — it does not touch any existing function or the route. `RosterRow`'s existing `min_grade`/`max_grade`/`grade`/`submission_status` fields are exactly what this needs; no changes to `fetch_roster_rows` or `RosterRow` itself.

- [ ] **Step 1: Write the failing tests**

Add `IntegrityRow` and `compute_cached_stats` to `tests/unit/test_gradebook.py`'s existing top-of-file `from submissions_checker.services.gradebook import (...)` block — do not add a new import line elsewhere in the file (a prior task on this branch had to fix exactly this PEP 8 mistake in a review round; avoid repeating it).

Append to `tests/unit/test_gradebook.py` (the file already has `_row()` and `_NOW` helpers from the prior branch's tests — reuse them):

```python
def _integrity_row(*, flagged: bool) -> IntegrityRow:
    return IntegrityRow(
        student_id=1,
        student_name="S",
        assignment_id=1,
        assignment_title="A",
        tab_switch=0,
        window_blur=0,
        force_fail=False,
        other_events=0,
        duration_seconds=100,
        median_seconds=100,
        duration_anomalous=False,
        severity="high" if flagged else None,
        flagged=flagged,
    )


def test_compute_cached_stats_average_mark_is_sum_over_sum_as_percent() -> None:
    rows = [
        _row(assignment_id=1, grade=40, max_grade=50),  # 80%
        _row(assignment_id=2, grade=10, max_grade=50),  # 20%
    ]
    stats = compute_cached_stats(rows, [], now=_NOW)
    # (40+10) / (50+50) * 100 = 50.0, NOT the plain mean of 80% and 20%
    assert stats.average_mark_pct == 50.0


def test_compute_cached_stats_average_mark_excludes_ungraded_pairs() -> None:
    rows = [_row(assignment_id=1, grade=40, max_grade=50), _row(assignment_id=2, grade=None)]
    stats = compute_cached_stats(rows, [], now=_NOW)
    assert stats.average_mark_pct == 80.0


def test_compute_cached_stats_average_mark_none_when_nothing_graded() -> None:
    rows = [_row(assignment_id=1, grade=None)]
    assert compute_cached_stats(rows, [], now=_NOW).average_mark_pct is None


def test_compute_cached_stats_pass_pct_is_per_work_not_per_student() -> None:
    # Two students, one assignment each: one passes, one doesn't. 1/2 = 50%,
    # regardless of whether either student is "fully" graded.
    rows = [
        _row(student_id=1, assignment_id=1, grade=80, min_grade=50),
        _row(student_id=2, assignment_id=1, grade=30, min_grade=50),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pass_pct == 50.0


def test_compute_cached_stats_pass_pct_counts_ungraded_as_not_passed() -> None:
    rows = [
        _row(student_id=1, assignment_id=1, grade=80, min_grade=50),
        _row(student_id=1, assignment_id=2, grade=None),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pass_pct == 50.0


def test_compute_cached_stats_pass_pct_zero_rows_is_zero() -> None:
    assert compute_cached_stats([], [], now=_NOW).pass_pct == 0.0


def test_compute_cached_stats_pending_review_matches_existing_rule() -> None:
    rows = [
        _row(assignment_id=1, grade=None, submission_status=SubmissionStatus.TESTING),
        _row(assignment_id=2, grade=None, submission_status=SubmissionStatus.COMPLETED),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pending_review_count == 1


def test_compute_cached_stats_cheating_pct_over_flagged_attempts() -> None:
    integrity = [_integrity_row(flagged=True), _integrity_row(flagged=False)]
    assert compute_cached_stats([], integrity, now=_NOW).cheating_pct == 50.0


def test_compute_cached_stats_cheating_pct_none_when_no_attempts() -> None:
    assert compute_cached_stats([], [], now=_NOW).cheating_pct is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen --extra dev --with redis pytest tests/unit/test_gradebook.py -v -k compute_cached_stats`
Expected: FAIL — `ImportError: cannot import name 'compute_cached_stats'`

- [ ] **Step 3: Implement**

Append to `src/submissions_checker/services/gradebook.py`:

```python
@dataclass(frozen=True)
class CachedSubjectStats:
    pending_review_count: int
    average_mark_pct: float | None
    pass_pct: float
    cheating_pct: float | None


def compute_cached_stats(
    roster_rows: list[RosterRow], integrity_rows: list[IntegrityRow], *, now: datetime
) -> CachedSubjectStats:
    graded = [(r.grade, r.max_grade) for r in roster_rows if r.grade is not None]
    graded_max_sum = sum(m for _, m in graded)
    average_mark_pct = (
        round(100 * sum(g for g, _ in graded) / graded_max_sum, 1) if graded_max_sum else None
    )

    total_pairs = len(roster_rows)
    passed_pairs = sum(1 for r in roster_rows if r.grade is not None and r.grade >= r.min_grade)
    pass_pct = round(100 * passed_pairs / total_pairs, 1) if total_pairs else 0.0

    pending_review_count = sum(
        1
        for r in roster_rows
        if r.grade is None
        and r.submission_status is not None
        and r.submission_status not in _TERMINAL_STATUSES
    )

    total_attempts = len(integrity_rows)
    flagged_attempts = sum(1 for r in integrity_rows if r.flagged)
    cheating_pct = (
        round(100 * flagged_attempts / total_attempts, 1) if total_attempts else None
    )

    return CachedSubjectStats(
        pending_review_count=pending_review_count,
        average_mark_pct=average_mark_pct,
        pass_pct=pass_pct,
        cheating_pct=cheating_pct,
    )
```

Note `now` is accepted for signature symmetry with `compute_stats` and future use (e.g. an overdue-aware stat later) but isn't used by any of the four current formulas — that's fine, don't manufacture a use for it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/unit/test_gradebook.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add src/submissions_checker/services/gradebook.py tests/unit/test_gradebook.py
git commit -m "Add compute_cached_stats for the scheduled-job stat cards"
```

---

### Task 3: `fetch_grid_rows` + `build_student_grid` — the Студенти tab's data

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Test: `tests/unit/test_gradebook.py` (pure `build_student_grid`), `tests/functional/test_gradebook.py` (DB-backed `fetch_grid_rows`)

**Interfaces:**
- Consumes: `cell_status` (existing, from the prior branch)
- Produces: `GridSourceRow`, `GridCell`, `GridColumn`, `GridRow`, `StudentGrid` dataclasses; `build_student_grid(rows: list[GridSourceRow]) -> StudentGrid` (pure); `async def fetch_grid_rows(db: AsyncSession, subject_id: int) -> list[GridSourceRow]` (DB). Consumed by Task 6 (route).

- [ ] **Step 1: Write the failing pure-function tests**

Add `GridSourceRow` and `build_student_grid` to `tests/unit/test_gradebook.py`'s existing top-of-file `from submissions_checker.services.gradebook import (...)` block (same import block Task 2 Step 1 just extended) — do not add a new import line elsewhere in the file.

Append to `tests/unit/test_gradebook.py`:

```python
def _grid_row(
    *,
    student_id: int = 1,
    student_name: str = "Student 1",
    group_name: str = "IT-21",
    assignment_id: int = 1,
    assignment_title: str = "Lab 1",
    min_grade: int = 0,
    grade: int | None = None,
    submission_status: SubmissionStatus | None = None,
    quiz_score: float | None = None,
    review_score: float | None = None,
) -> GridSourceRow:
    return GridSourceRow(
        student_id=student_id,
        student_name=student_name,
        group_name=group_name,
        assignment_id=assignment_id,
        assignment_title=assignment_title,
        min_grade=min_grade,
        grade=grade,
        submission_status=submission_status,
        quiz_score=quiz_score,
        review_score=review_score,
    )


def test_build_student_grid_columns_in_first_seen_order() -> None:
    rows = [_grid_row(assignment_id=2), _grid_row(assignment_id=1)]
    grid = build_student_grid(rows)
    assert [c.assignment_id for c in grid.columns] == [2, 1]


def test_build_student_grid_cell_carries_quiz_and_review_scores() -> None:
    rows = [_grid_row(grade=90, min_grade=50, quiz_score=88.0, review_score=95.0)]
    grid = build_student_grid(rows)
    cell = grid.rows[0].cells[1]
    assert cell.quiz_score == 88.0
    assert cell.review_score == 95.0
    assert cell.status == "passed"


def test_build_student_grid_total_sums_final_grade_not_sub_marks() -> None:
    rows = [
        _grid_row(assignment_id=1, grade=80, quiz_score=10.0, review_score=10.0),
        _grid_row(assignment_id=2, grade=20, quiz_score=90.0, review_score=90.0),
    ]
    grid = build_student_grid(rows)
    assert grid.rows[0].total == 100  # 80 + 20, not derived from quiz/review scores


def test_build_student_grid_carries_group_name_and_sorts_by_student_name() -> None:
    rows = [
        _grid_row(student_id=2, student_name="Zed", group_name="IT-22"),
        _grid_row(student_id=1, student_name="Anna", group_name="IT-21"),
    ]
    grid = build_student_grid(rows)
    assert [r.student_name for r in grid.rows] == ["Anna", "Zed"]
    assert grid.rows[0].group_name == "IT-21"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev --with redis pytest tests/unit/test_gradebook.py -v -k build_student_grid`
Expected: FAIL — `ImportError: cannot import name 'GridSourceRow'`

- [ ] **Step 3: Implement the dataclasses + pure function**

Append to `src/submissions_checker/services/gradebook.py`:

```python
@dataclass(frozen=True)
class GridSourceRow:
    student_id: int
    student_name: str
    group_name: str
    assignment_id: int
    assignment_title: str
    min_grade: int
    grade: int | None
    submission_status: SubmissionStatus | None
    quiz_score: float | None
    review_score: float | None


@dataclass(frozen=True)
class GridCell:
    status: CellStatus
    grade: int | None
    quiz_score: float | None
    review_score: float | None


@dataclass(frozen=True)
class GridColumn:
    assignment_id: int
    title: str


@dataclass(frozen=True)
class GridRow:
    student_id: int
    student_name: str
    group_name: str
    cells: dict[int, GridCell]
    total: int | None


@dataclass(frozen=True)
class StudentGrid:
    columns: list[GridColumn]
    rows: list[GridRow]


def build_student_grid(rows: list[GridSourceRow]) -> StudentGrid:
    columns: list[GridColumn] = []
    seen_assignments: set[int] = set()
    for r in rows:
        if r.assignment_id not in seen_assignments:
            seen_assignments.add(r.assignment_id)
            columns.append(GridColumn(r.assignment_id, r.assignment_title))

    cells_by_student: dict[int, dict[int, GridCell]] = {}
    meta_by_student: dict[int, tuple[str, str]] = {}
    for r in rows:
        cells_by_student.setdefault(r.student_id, {})[r.assignment_id] = GridCell(
            status=cell_status(r.grade, r.min_grade, r.submission_status),
            grade=r.grade,
            quiz_score=r.quiz_score,
            review_score=r.review_score,
        )
        meta_by_student[r.student_id] = (r.student_name, r.group_name)

    grid_rows = []
    for student_id, cells in cells_by_student.items():
        student_name, group_name = meta_by_student[student_id]
        grades = [c.grade for c in cells.values() if c.grade is not None]
        total = sum(grades) if grades else None
        grid_rows.append(GridRow(student_id, student_name, group_name, cells, total))
    grid_rows.sort(key=lambda row: row.student_name)

    return StudentGrid(columns=columns, rows=grid_rows)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/unit/test_gradebook.py -v`
Expected: PASS (all)

- [ ] **Step 5: Write the failing functional test for `fetch_grid_rows`**

Extend the file's existing top-of-file `from submissions_checker.services.gradebook import fetch_integrity_rows, fetch_roster_rows` line to also import `fetch_grid_rows` — add it to that same import statement, do not add a second, separate import line elsewhere in the file (a prior task on this branch had to fix exactly this PEP 8 mistake in a review round; avoid repeating it).

Append to `tests/functional/test_gradebook.py`. This needs `SubjectsAssignment.min_grade` set and a `Submission.grade_breakdown` value — extend the file's existing `_make_submission` helper call sites or add `grade_breakdown` inline via direct model construction:

```python
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
    await _make_student_assignment(db, student.id, a1.id)  # no submission at all

    rows = await fetch_grid_rows(db, subject.id)

    assert rows[0].quiz_score is None
    assert rows[0].review_score is None
```

Check whether the existing `make_group` fixture in `tests/functional/conftest.py` accepts a `name=` kwarg (it does, per Task 5's earlier code in this same plan family — `make_group(name=...)`) and whether `make_student` accepts a `group=` kwarg to pin a student to a specific group (it does — see `tests/functional/conftest.py`'s `make_student` fixture signature).

- [ ] **Step 6: Run to verify they fail**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v -k fetch_grid_rows`
Expected: FAIL — `ImportError: cannot import name 'fetch_grid_rows'`

- [ ] **Step 7: Implement `fetch_grid_rows`**

This mirrors `fetch_roster_rows` (already in this file) closely, adding a `Group` join and selecting `Submission.grade_breakdown`. `Group` needs importing: add `from submissions_checker.db.models.group import Group` to the imports.

Append to `src/submissions_checker/services/gradebook.py`:

```python
async def fetch_grid_rows(db: AsyncSession, subject_id: int) -> list[GridSourceRow]:
    """One row per (enrolled real student, subject assignment) pair, with the
    student's group name and their latest submission's quiz/review component
    scores (Submission.grade_breakdown's quiz_score/quality_score — see
    services.grading.GradeBreakdown) for the Студенти tab's grid.

    Same "latest submission per student_assignment" pattern as fetch_roster_rows,
    duplicated rather than shared because this query needs two extra joins
    (Group, and the grade_breakdown column) that fetch_roster_rows' caller (the
    scheduled job) has no use for.
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
            Group.name.label("group_name"),
            SubjectsAssignment.id.label("assignment_id"),
            SubjectsAssignment.title.label("assignment_title"),
            SubjectsAssignment.min_grade,
            StudentAssignment.grade,
            Submission.status.label("submission_status"),
            Submission.grade_breakdown,
        )
        .select_from(SubjectsStudents)
        .join(Student, Student.id == SubjectsStudents.student_id)
        .join(Group, Group.id == Student.group_id)
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
    rows = []
    for row in result:
        breakdown = row.grade_breakdown or {}
        rows.append(
            GridSourceRow(
                student_id=row.student_id,
                student_name=row.student_name,
                group_name=row.group_name,
                assignment_id=row.assignment_id,
                assignment_title=row.assignment_title,
                min_grade=row.min_grade,
                grade=row.grade,
                submission_status=row.submission_status,
                quiz_score=breakdown.get("quiz_score"),
                review_score=breakdown.get("quality_score"),
            )
        )
    return rows
```

- [ ] **Step 8: Run to verify they pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v`
Expected: PASS (all, old and new)

- [ ] **Step 9: Commit**

```bash
git add src/submissions_checker/services/gradebook.py tests/unit/test_gradebook.py tests/functional/test_gradebook.py
git commit -m "Add fetch_grid_rows and build_student_grid for the reworked Студенти grid"
```

---

### Task 4: Scheduled job — `subject_stats_refresh`

**Files:**
- Create: `src/submissions_checker/workers/scheduled/subject_stats_refresh.py`
- Modify: `src/submissions_checker/core/scheduler.py`
- Modify: `src/submissions_checker/core/config.py`
- Test: `tests/integration/test_subject_stats_refresh.py`

**Interfaces:**
- Consumes: `fetch_roster_rows`, `fetch_integrity_rows`, `compute_cached_stats` (Task 2), `SubjectGradebookStats` (Task 1)
- Produces: `async def refresh_subject_gradebook_stats() -> None`, registered as job id `"subject_stats_refresh"`. Nothing in this plan calls it directly except the scheduler and this task's own test — Task 6 only ever *reads* the table it writes.

- [ ] **Step 1: Add the config setting**

In `src/submissions_checker/core/config.py`, add near `metrics_refresh_interval` (same section):

```python
    # Subject gradebook stats: how often the cached stat-card numbers (average
    # mark, pass %, pending review, cheating %) are recomputed per subject.
    subject_stats_refresh_interval: int = 300
```

- [ ] **Step 2: Write the failing integration test**

Read `tests/integration/test_teacher_digest.py` in full first — this test file's `db_session` fixture, `_seed`-style helper pattern, and `monkeypatch.setattr(<module>, "get_session", fake_get_session)` pattern are exactly what this task's test reuses; **do not invent a different fixture setup**.

Create `tests/integration/test_subject_stats_refresh.py`:

```python
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
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.subject import SubjectsStudents
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
    teacher_owner_id = 1  # no FK to users in this test's minimal seed; subjects.owner_id has none required
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
        await db_session.execute(
            select(SubjectGradebookStats).where(SubjectGradebookStats.subject_id == subject.id)
        )
    ).scalars().all()
    assert len(rows) == 1
```

Confirm `Subject.status` defaults to something the job's subject-selection query includes (check `db/models/subject.py` for the default `SubjectStatus` — if the job filters `status == ACTIVE`, these fixture subjects need `status=SubjectStatus.ACTIVE` set explicitly, or omit the status filter from the job's query if `Subject`'s default already is `ACTIVE`; verify before writing Step 4 below and adjust the test's `Subject(...)` calls accordingly if needed).

- [ ] **Step 3: Run to verify it fails**

Run: `uv run --frozen --extra dev --with redis pytest tests/integration/test_subject_stats_refresh.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'submissions_checker.workers.scheduled.subject_stats_refresh'`

- [ ] **Step 4: Implement the job**

Read `src/submissions_checker/workers/scheduled/teacher_digest_processor.py` in full first for the exact advisory-lock (`pg_try_advisory_lock`/`pg_advisory_unlock` via raw `text(...)`) try/finally shape — this task's job follows that structure exactly, with a new, distinct lock id (existing ones are `7919` outbox, `7927` teacher digest — use `7935` here).

Create `src/submissions_checker/workers/scheduled/subject_stats_refresh.py`:

```python
"""Scheduled job: precompute the four Панель stat-card numbers per subject.

Runs on subject_stats_refresh_interval (default 300s). The teacher_subject
route reads subject_gradebook_stats live but never computes these numbers
itself — see services.gradebook.compute_cached_stats for the calculation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text

from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
from submissions_checker.db.session import get_session
from submissions_checker.services.gradebook import (
    compute_cached_stats,
    fetch_integrity_rows,
    fetch_roster_rows,
)

logger = get_logger(__name__)

# PostgreSQL advisory lock id for this job (distinct from outbox 7919, teacher digest 7927)
SUBJECT_STATS_REFRESH_LOCK_ID = 7935


async def refresh_subject_gradebook_stats() -> None:
    """Recompute and upsert subject_gradebook_stats for every subject."""
    try:
        async with get_session() as db:
            lock_result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": SUBJECT_STATS_REFRESH_LOCK_ID},
            )
            if not lock_result.scalar():
                logger.info("subject_stats_refresh_lock_not_acquired")
                return

            try:
                subject_ids = (await db.execute(select(Subject.id))).scalars().all()
                now = datetime.now(UTC)
                for subject_id in subject_ids:
                    roster_rows = await fetch_roster_rows(db, subject_id)
                    integrity_rows = await fetch_integrity_rows(db, subject_id)
                    stats = compute_cached_stats(roster_rows, integrity_rows, now=now)

                    existing = await db.get(SubjectGradebookStats, subject_id)
                    if existing is None:
                        db.add(
                            SubjectGradebookStats(
                                subject_id=subject_id,
                                pending_review_count=stats.pending_review_count,
                                average_mark_pct=stats.average_mark_pct,
                                pass_pct=stats.pass_pct,
                                cheating_pct=stats.cheating_pct,
                                computed_at=now,
                            )
                        )
                    else:
                        existing.pending_review_count = stats.pending_review_count
                        existing.average_mark_pct = stats.average_mark_pct
                        existing.pass_pct = stats.pass_pct
                        existing.cheating_pct = stats.cheating_pct
                        existing.computed_at = now
                    await db.commit()

                logger.info("subject_stats_refresh_completed", subjects=len(subject_ids))

            finally:
                await db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": SUBJECT_STATS_REFRESH_LOCK_ID},
                )

    except Exception as exc:  # noqa: BLE001 — matches teacher_digest_processor's top-level catch
        logger.error("subject_stats_refresh_error", error=str(exc))
```

If the test from Step 2 needs subjects filtered by `status == ACTIVE`, add `.where(Subject.status == SubjectStatus.ACTIVE)` to the `select(Subject.id)` query and import `SubjectStatus` from `submissions_checker.db.models.enums` — only do this if you confirmed in Step 2 that `Subject`'s default status isn't already what the fixtures produce, to avoid the test silently selecting zero subjects.

- [ ] **Step 5: Register the job**

In `src/submissions_checker/core/scheduler.py`, inside `_register_jobs()`, add the import alongside the other worker imports and a new `scheduler.add_job(...)` call alongside the existing three, following the exact same shape as the `metrics_refresh` registration (interval from settings, `replace_existing=True`, `max_instances=1`):

```python
    from submissions_checker.workers.scheduled.subject_stats_refresh import (
        refresh_subject_gradebook_stats,
    )
```

```python
    # Subject gradebook stats — cached Панель stat-card numbers
    scheduler.add_job(
        refresh_subject_gradebook_stats,
        trigger=IntervalTrigger(seconds=settings.subject_stats_refresh_interval),
        id="subject_stats_refresh",
        name="Refresh subject gradebook stats",
        replace_existing=True,
        max_instances=1,
    )

    logger.info(
        "Registered subject stats refresh job (interval: %ss)",
        settings.subject_stats_refresh_interval,
    )
```

- [ ] **Step 6: Run to verify tests pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/integration/test_subject_stats_refresh.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add src/submissions_checker/workers/scheduled/subject_stats_refresh.py src/submissions_checker/core/scheduler.py src/submissions_checker/core/config.py tests/integration/test_subject_stats_refresh.py
git commit -m "Add the subject_stats_refresh scheduled job"
```

---

### Task 5: Search-by-email + enroll-by-search routes

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py`
- Test: `tests/functional/test_gradebook.py` (or a new `tests/functional/test_subject_search_enroll.py` if `test_gradebook.py` is getting large — implementer's call, keep it in `test_gradebook.py` unless it's genuinely awkward there)

**Interfaces:**
- Produces: `GET /teacher/subjects/{subject_id}/students/search?q=` (JSON: `[{"id": int, "full_name": str, "email": str}, ...]`), `POST /teacher/subjects/{subject_id}/students/enroll-by-search` (form: `student_id`, `variant` optional/required per subject config). Consumed by Task 6's template (Операції tab's JS calls these).

This task is purely additive — two new routes, nothing existing changes. Safe to implement independently of Tasks 2-4.

- [ ] **Step 1: Write the failing tests**

Append to `tests/functional/test_gradebook.py`:

```python
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
    from submissions_checker.db.models.enums import UserRole

    other = await make_user(role=UserRole.TEACHER, username="other-search")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}/students/search?q=abc")
    assert resp.status_code == 403


async def test_enroll_by_search_creates_enrollment(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, code="lab1")
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
        data={"student_id": str(student.id)},  # no variant
    )
    assert resp.status_code == 422
```

Check `_make_assignment`'s signature in this file (from the prior branch's tests) accepts a `config: dict | None = None` kwarg — if it doesn't, add one matching the pattern already used for `deadline`/`min_grade`/`max_grade` kwargs in that same helper.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v -k "search_students or enroll_by_search"`
Expected: FAIL — 404s (routes don't exist yet)

- [ ] **Step 3: Implement the routes**

In `src/submissions_checker/api/routes/teacher_portal.py`, near the existing `enroll_student` route (~line 1123), add:

```python
@router.get("/subjects/{subject_id}/students/search")
async def search_students_by_email(
    subject_id: int,
    q: str,
    db: DBSession,
    current_user: TeacherUser,
) -> list[dict[str, Any]]:
    """Autocomplete source for the search-enroll flow. 3+ chars, ILIKE on email."""
    await require_subject_access(db, subject_id, current_user)
    if len(q) < 3:
        raise HTTPException(status_code=422, detail="Query must be at least 3 characters")

    result = await db.execute(
        select(Student.id, Student.full_name, Student.email)
        .where(Student.type == EntityType.REAL, Student.email.ilike(f"%{q}%"))
        .order_by(Student.full_name)
        .limit(10)
    )
    return [
        {"id": row.id, "full_name": row.full_name, "email": row.email} for row in result
    ]


@router.post("/subjects/{subject_id}/students/enroll-by-search")
async def enroll_student_by_search(
    subject_id: int,
    student_id: Annotated[int, Form()],
    db: DBSession,
    current_user: TeacherUser,
    variant: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Enroll one student found via search, reusing the same enrollment logic
    button-enroll and CSV-enroll already share (_ensure_assignment_rows)."""
    await require_subject_access(db, subject_id, current_user)

    sa_rows_result = await db.execute(
        select(SubjectsAssignment.id, SubjectsAssignment.config).where(
            SubjectsAssignment.subject_id == subject_id
        )
    )
    sa_rows = sa_rows_result.all()
    needs_variant = any((row.config or {}).get("variants_required") for row in sa_rows)
    clean_variant = (variant or "").strip() or None
    if needs_variant and not clean_variant:
        raise HTTPException(status_code=422, detail="This subject requires a variant")

    existing = await db.execute(
        select(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
        await _ensure_assignment_rows(
            db, student_id, [row.id for row in sa_rows], clean_variant
        )
        await audit(
            db,
            action="enroll_student_by_search",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            subject_id=subject_id,
            student_id=student_id,
        )
        await db.commit()

    return RedirectResponse(url=f"/teacher/subjects/{subject_id}", status_code=303)
```

Check whether `Annotated`/`Form` are already imported at the top of `teacher_portal.py` (this repo's FastAPI form-handling convention elsewhere in the same file, e.g. the CSV import route uses `UploadFile` directly rather than `Form` fields — check how `provision_test_student`'s per-assignment `variant_{code}` fields are read, since that's this file's existing pattern for reading form fields on a POST route, and match it if it differs from plain `Annotated[..., Form()]`).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v`
Expected: PASS (all, old and new)

Also run the existing auth/isolation regression suites since this touches `teacher_portal.py`:
Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_teacher_portal.py tests/functional/test_portal_detail_pages.py -v`
Expected: PASS (no regressions)

- [ ] **Step 5: Commit**

```bash
git add src/submissions_checker/api/routes/teacher_portal.py tests/functional/test_gradebook.py
git commit -m "Add search-by-email enroll routes for the Операції tab"
```

---

### Task 6: Route + template integration — the reworked tabs

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_subject()`)
- Modify: `templates/teacher_subject.html` (full tab-content rework)
- Modify: `i18n/uk.yml`
- Test: `tests/functional/test_gradebook.py`

**Interfaces:**
- Consumes: `SubjectGradebookStats` (Task 1), `fetch_grid_rows`/`build_student_grid` (Task 3), the two new routes' URLs (Task 5)
- Produces: the finished rework. Nothing downstream depends on this task's output within this plan — Task 7 only deletes now-dead code this task stops referencing.

This is the largest task in this plan: it's the one place where "old context keys go away, new ones appear" has to happen atomically — the route and template can't be split further without an intermediate broken state. Read `templates/teacher_subject.html` and `teacher_subject()` (currently ~line 284-380 in `teacher_portal.py`) in full before starting.

- [ ] **Step 1: Write the failing tests**

Add this import to the top of `tests/functional/test_gradebook.py` (it isn't there yet — everything else these tests use, `Submission`/`datetime`/`UTC`/`SubmissionSourceType`/`SubmissionStatus`, is already imported from the prior branch's tests):

```python
from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats
```

Append to `tests/functional/test_gradebook.py`:

```python
async def test_teacher_subject_has_four_reworked_tabs(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    for target in ["panel", "assignments", "students", "operations"]:
        assert f'data-tab-target="{target}"' in resp.text
        assert f'id="tab-panel-{target}"' in resp.text


async def test_panel_tab_shows_placeholder_when_stats_not_yet_computed(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    assert "—" in resp.text  # no SubjectGradebookStats row exists for this subject yet


async def test_panel_tab_shows_cached_stats_when_present(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    db.add(
        SubjectGradebookStats(
            subject_id=subject.id,
            pending_review_count=3,
            average_mark_pct=77.5,
            pass_pct=60.0,
            cheating_pct=12.5,
            computed_at=datetime.now(UTC),
        )
    )
    await db.commit()
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert "77.5" in resp.text
    assert "60.0%" in resp.text
    assert "12.5%" in resp.text
    assert ">3<" in resp.text  # pending review count


async def test_assignments_tab_shows_pending_review_count_per_task(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Lab X", code="labx")
    student = await make_student()
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id)
    await _make_submission(db, sa.id, status=SubmissionStatus.AWAITING_TEACHER_REVIEW)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert "Lab X" in resp.text
    assert ">1<" in resp.text  # one pending review for this assignment


async def test_students_tab_shows_grid_with_quiz_and_review_columns(
    client: AsyncClient, db: AsyncSession, teacher, make_student
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    a1 = await _make_assignment(db, subject.id, title="Lab Y", code="laby", min_grade=50)
    student = await make_student(full_name="Grid Tab Student")
    await _enroll(db, subject.id, student.id)
    sa = await _make_student_assignment(db, student.id, a1.id, grade=88)
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=SubmissionStatus.COMPLETED,
        grade_breakdown={"quiz_score": 70.0, "quality_score": 95.0},
    )
    db.add(sub)
    await db.commit()
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert "Grid Tab Student" in resp.text
    assert "Lab Y" in resp.text
    assert ">70.0<" in resp.text or "70.0" in resp.text
    assert ">95.0<" in resp.text or "95.0" in resp.text
    assert "violation-dot" not in resp.text  # dropped per this rework


async def test_operations_tab_has_search_enroll_and_feedback_button(
    client: AsyncClient, db: AsyncSession, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    resp = await client.get(f"/teacher/subjects/{subject.id}")
    assert 'data-student-search' in resp.text
    # The feedback button's href is stable across all three of its states
    # (no-semester / already-sent / send-form) — asserting on it, rather than
    # on button text that varies by state, confirms the button moved into
    # this tab without depending on which state fired for this fixture.
    assert f'href="/teacher/subjects/{subject.id}/feedback"' in resp.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v -k "reworked_tabs or panel_tab or assignments_tab_shows_pending or students_tab_shows_grid or operations_tab"`
Expected: FAIL (old tab structure, old data keys)

- [ ] **Step 3: Rewrite the route**

In `teacher_portal.py`'s `teacher_subject()`:

- Remove the calls to `fetch_roster_rows`, `compute_stats`, `build_grid`, `fetch_integrity_rows`, and the `integrity_by_cell` dict construction (these still exist in `gradebook.py` — the scheduled job uses two of them — this task just stops the *route* calling them).
- Add: fetch the cached stats row (`await db.get(SubjectGradebookStats, subject_id)` — `None` if the job hasn't run yet for this subject), fetch grid rows and build the grid (`grid_rows = await fetch_grid_rows(db, subject_id); student_grid = build_student_grid(grid_rows)`), and derive per-assignment pending-review counts from `grid_rows` directly (group by `assignment_id`, count where `grade is None` and `submission_status` is set and not terminal — reuse the same `_TERMINAL_STATUSES` set gradebook.py exports, or inline the check since `SubmissionStatus.COMPLETED`/`FAILED` are already imported in this route file).
- Update `default_tab`: `"operations" if (enroll_result or test_student_flash or feedback_sent or feedback_error) else "panel"`.
- Update the `render(...)` context dict: replace `gradebook_stats`/`gradebook_grid`/`integrity_rows`/`integrity_by_cell` with `cached_stats` (the `SubjectGradebookStats | None`), `student_grid` (`StudentGrid`), `task_pending_counts` (`dict[int, int]` keyed by `assignment_id`).

Add the new imports to the existing `from submissions_checker.services.gradebook import (...)` block: `build_student_grid`, `fetch_grid_rows`. Add `from submissions_checker.db.models.subject_gradebook_stats import SubjectGradebookStats` to the model imports.

- [ ] **Step 4: Rewrite the template**

Rewrite `templates/teacher_subject.html`'s tab bar and all four panels. Reuse everything that doesn't change: the page header, the feedback-request button's *markup* (just relocated), the enroll-CSV card markup, the Test Student panel markup, the students-list markup (unchanged, stays on... no — per the spec, the plain enrolled-students list is gone; Студенти is now the grid tab only), the assignments-list markup (gets the pending-badge addition), the tab-switcher `<script>` IIFE (reuse as-is, just the panel/trigger ids change from `overview/students/assignments/grades` to `operations/students/assignments/panel` — **pick consistent ids**: this plan uses `panel`/`assignments`/`students`/`operations` as the four `data-tab-target`/`id="tab-panel-*"` values, matching the test assertions in Step 1).

Structure:

```
tab bar: Панель | Завдання | Студенти | Операції
  data-tab-target="panel" / "assignments" / "students" / "operations"

tab-panel-panel:
  four stat cards, each reading `cached_stats.<field>` with a "—" fallback
  when `cached_stats` is None or a specific field is None:
    {% if cached_stats and cached_stats.average_mark_pct is not none %}{{ cached_stats.average_mark_pct }}{% else %}—{% endif %}
  (same pattern for pass_pct, cheating_pct, pending_review_count — pending_review_count
   and pass_pct are never None on a row that exists, only average_mark_pct/cheating_pct can be)

tab-panel-assignments:
  existing list markup, plus per <li> a small badge:
    {% set pending = task_pending_counts.get(a.id, 0) %}
    {% if pending %}<span class="...">{{ pending }} {{ vocab.teacher.pending_review_badge }}</span>{% endif %}
  and darken the existing `text-slate-400` on the deadline/points line to `text-slate-500`
  (or `text-slate-600` if `text-slate-500` still reads faded against this repo's off-white background — implementer's visual judgment, cite the exact class you land on in the commit message)

tab-panel-students:
  new grid: sticky ПІБ | Група | Разом columns, then per assignment a two-cell-wide
  header ("colspan=2") with the assignment title, and Тест/Огляд sub-headers underneath
  in a second header row. Reuse the sticky-column CSS pattern and overflow-x-auto
  wrapper from the prior branch's grid (git log/blame templates/teacher_subject.html
  around the old <table> for the exact sticky classes, even though that whole block
  is being replaced — same visual technique applies). Cell content per sub-column:
  quiz_score / review_score, "—" when None, tinted by cell.status (reuse the same
  passed/failed/pending/not_submitted color classes the prior grid used) applied to
  the whole 2-column cell group's background, not per sub-column.

tab-panel-operations:
  existing enroll-CSV card + Test Student panel, unchanged content, PLUS:
  - a new search input: `<input data-student-search ...>` with a debounced (300ms)
    vanilla-JS keyup handler (no framework) that, once the value is 3+ chars, GETs
    `/teacher/subjects/{{ subject.id }}/students/search?q=<value>`, renders results
    into a dropdown `<ul>`; clicking a result reveals a variant `<input>` (only if
    `any(a.config.get('variants_required') for a in assignments)` — compute this
    once at the top of the panel as a Jinja `{% set %}`) and an Enroll button that
    POSTs `student_id` (+ `variant` if shown) to
    `/teacher/subjects/{{ subject.id }}/students/enroll-by-search`, then reloads
    the page (simplest: a plain `<form>` POST, not fetch — matches this repo's
    existing enroll-button pattern, which is a plain form POST + redirect, not AJAX)
  - the feedback-request button (all three states: no-semester / already-sent /
    send-form) and the `feedback_sent`/`feedback_error` banners, moved here
    verbatim from the page header/top-of-content area
```

The search-results dropdown and variant reveal need *some* client interactivity beyond a plain form (the debounce + populate-dropdown-from-JSON part), so this one piece of the Операції tab does need a small `<script>` addition — extend the existing tab-switcher IIFE (same single `<script>` tag convention established on the prior branch — do not add a second `<script>` block) with a `fetch()`-based handler.

Remove the old integrity-table markup, the old grid's violation-dot markup and its `<script>` handlers (the `flaggedToggle`/`.violation-dot` blocks), and the old `integrity_*` vocab keys from `i18n/uk.yml` that only the removed markup used (keep `integrity_severity_high`/`integrity_severity_medium` etc. ONLY if you're certain nothing else references them — grep the whole template first).

Add new vocab keys to `i18n/uk.yml` (teacher block, before `admin:`): `tab_panel` (Панель), rename or add `tab_operations` (Операції) — check whether `tab_overview`/`tab_students`/`tab_assignments`/`tab_grades` from the prior branch should be renamed in place or replaced; `pending_review_badge`, `panel_stat_average_mark`, `panel_stat_pass_pct`, `panel_stat_cheating_pct`, `panel_stat_pending`, `grid_col_total`, `grid_col_group`, `grid_subcol_quiz`, `grid_subcol_review`, `students_search_placeholder`, `students_search_no_results`, `students_search_variant_label`, `students_search_enroll_button`.

- [ ] **Step 5: Run to verify they pass**

Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_gradebook.py -v`
Expected: PASS (all)

Run the full regression check — this task touches the most-tested route/template pair in the app:
Run: `uv run --frozen --extra dev --with redis pytest tests/functional/test_teacher_portal.py tests/functional/test_portal_detail_pages.py -v`
Expected: PASS. If any test from the prior branch specifically asserted on now-removed markup (old tab ids `overview`/`grades`, the integrity table, the old single-mark grid), fix those specific assertions to match the new structure — do not weaken them, update them to check the equivalent new thing.

- [ ] **Step 6: Commit**

```bash
git add src/submissions_checker/api/routes/teacher_portal.py templates/teacher_subject.html i18n/uk.yml tests/functional/test_gradebook.py
git commit -m "Rework subject tabs: Панель/Завдання/Студенти/Операції"
```

---

### Task 7: Cleanup — delete superseded code

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py`
- Modify: `tests/unit/test_gradebook.py`, `tests/functional/test_gradebook.py`
- Test: full suite

**Interfaces:** None produced — this task only removes code nothing calls anymore.

- [ ] **Step 1: Confirm nothing references the old symbols**

Run: `grep -rn "compute_stats\|GradebookStats\|build_grid\b\|GradebookGrid\|GradebookRow\|GradebookColumn\|GradebookCell" src/ templates/ tests/`
Expected: hits only inside `gradebook.py`'s own definitions and their own tests (the ones about to be deleted). If `teacher_portal.py` or `teacher_subject.html` still reference any of these, Task 6 wasn't fully completed — stop and fix Task 6 first, don't delete code still in use.

- [ ] **Step 2: Delete the dead code**

Remove from `src/submissions_checker/services/gradebook.py`: `GradebookStats`, `compute_stats`, `GradebookCell`, `GradebookColumn`, `GradebookRow`, `GradebookGrid`, `build_grid`.

Remove their corresponding tests from `tests/unit/test_gradebook.py` (the `test_compute_stats_*` and `test_build_grid_*` functions from the prior branch) and any now-orphaned test-only helpers those tests used exclusively (check `_row()` is still used by `compute_cached_stats`'s tests before removing anything it depends on — it almost certainly still is).

Also remove any now-dead functional tests referencing the old grid shape or the integrity table from `tests/functional/test_gradebook.py` if Task 6 didn't already replace them in place.

- [ ] **Step 3: Run the full suite**

Run: `uv run --frozen --extra dev --with redis pytest -q --ignore=tests/e2e`
Expected: all pass, no `ImportError`s, no orphaned references.

Run: `uv run --frozen ruff check src/ tests/` and `uv run --frozen ruff format --check src/ tests/` and `uv run --frozen mypy src/`
Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add src/submissions_checker/services/gradebook.py tests/unit/test_gradebook.py tests/functional/test_gradebook.py
git commit -m "Remove the superseded single-grade grid and live-computed stats code"
```

---

### Task 8: Final verification + manual browser check

**Files:** None modified — verification only.

- [ ] **Step 1: Full suite, lint, mypy**

Run: `uv run --frozen --extra dev --with redis pytest -q --ignore=tests/e2e`
Run: `uv run --frozen ruff check src/ tests/`
Run: `uv run --frozen ruff format --check src/ tests/`
Run: `uv run --frozen mypy src/`
Expected: all clean.

- [ ] **Step 2: Manual browser check**

`docker compose -p submissions-checker up -d` from the worktree (reuses the cached image, same as the prior verification round in this session). Log in as `teacher`/`teacher123`, open a subject the teacher owns, and confirm: tab order is Панель/Завдання/Студенти/Операції; Панель shows either real numbers or "—" placeholders depending on whether the scheduled job has run (wait ~`subject_stats_refresh_interval` seconds, or manually insert a `subject_gradebook_stats` row via `psql` to see the populated state without waiting); Завдання shows a pending-review count next to any assignment with ungraded, submitted work; Студенти shows the grid with visible Тест/Огляд sub-columns and no violation dots anywhere; Операції has the search box (type 3+ characters of a known student's email, confirm a dropdown appears, pick one, confirm the variant field only appears when the subject actually has a `variants_required` assignment, enroll, confirm the student now appears in Студенти) and the feedback button relocated out of the header.

- [ ] **Step 3: Stop and hand back**

Per this session's established ruling, commits happen normally on the worktree branch — nothing here is held back. Report what was implemented and verified, and let the user review live before any merge/push decision.

---

## Self-Review

**Spec coverage:** scheduled job + cache table (Tasks 1, 2, 4) · Панель's four cards and their exact formulas (Tasks 2, 6) · Завдання pending badge + contrast fix (Task 6) · Студенти grid with quiz/review sub-marks (Tasks 3, 6) · Операції rename/reorder, search-enroll, relocated feedback button (Tasks 5, 6) · integrity table and violation-dot removal (Task 6 removes the markup, Task 7 removes the dead code) · non-goals (no CSV export, no inline editing, task-level pages untouched) — never introduced anywhere in this plan.

**Placeholder scan:** none — every step has literal, complete code. (An earlier draft of Task 6 Step 1 had a placeholder assertion for the feedback-button test; fixed to assert on the always-present `view_feedback` link's stable href instead.)

**Type consistency:** `GridSourceRow`/`GridCell`/`GridColumn`/`GridRow`/`StudentGrid` field names are used identically across Tasks 3 and 6 (`cells.get(col.assignment_id)`, `.quiz_score`, `.review_score`, `.total`, `.group_name`). `CachedSubjectStats` field names (`pending_review_count`, `average_mark_pct`, `pass_pct`, `cheating_pct`) match `SubjectGradebookStats`'s column names exactly (Task 1 ↔ Task 2 ↔ Task 4 ↔ Task 6) and match the template's field access in Task 6.
