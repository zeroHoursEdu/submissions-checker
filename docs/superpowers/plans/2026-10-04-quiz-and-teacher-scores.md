# Quiz Any Time + Teacher Scores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `review_mode: quiz_and_teacher_scores` — the student opens the quiz without uploading anything, the teacher types per-criterion points on the assignment board, and the grade is quiz points + teacher points with a visible breakdown.

**Architecture:** A quiz without an upload is anchored on an upload-less `Submission(source_type=QUIZ_ONLY)` created when the student first opens the quiz, so the whole existing quiz machinery (attempt caps, grants, disputes, squads, anti-cheat) is reused unchanged. Teacher points live in `students_assignments.teacher_scores` (JSONB). A pure module `services/teacher_scores.py` owns the criteria, validation and the grade formula; `finalize_grade` branches into it for this mode.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL, Jinja/Tailwind, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-10-04-quiz-and-teacher-scores-design.md`

## Global Constraints

- Mode string is exactly `quiz_and_teacher_scores`; the new enum value is exactly `QUIZ_ONLY`; the new column is `students_assignments.teacher_scores` (JSONB, NULL).
- Config: `grading.quiz_points` (int ≥ 0), `grading.teacher_criteria: [{key, title, max, optional?}]`; `key` matches `[a-z0-9_]+`, unique; `max` int > 0; `quiz_points + Σmax == max_grade − min_grade`.
- Quiz is a gate: no passed attempt ⇒ no grade. Optional criterion empty ⇒ 0. Required criterion empty ⇒ no grade.
- `quiz_awarded = round_half_up(quiz_pct/100 * quiz_points)`; squads use ceil (existing rule).
- Every other review mode behaves exactly as before.
- Deviation from spec §2 (agreed simplification): no separate `POST …/quiz/open`; the existing `GET /portal/subjects/{id}/assignments/{sa}/quiz` start route (already the «Почати тест» link) creates the QUIZ_ONLY submission under a row lock.
- Migration is purely additive (prod has live data; 2 replicas roll one at a time).
- Transitions only via `state_machine.transition()`; anything that changes status/grade gets an audit row.
- UI strings in `i18n/uk.yml` under `vocab.*`; Ukrainian.
- Always `uv run --frozen`. Full suite: `uv run --frozen --extra dev pytest -q`; lint: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/`; types: `uv run --frozen mypy src/`.
- Commits: imperative subject ≤72 chars, body explains why, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Double click / two tabs on «Почати тест» — exactly one QUIZ_ONLY submission (row lock on `students_assignments`). Test in Task 5.
2. Teacher types `"abc"`, `-1`, `6` for a `max: 5` field, or posts a key not in the config — 422 with nothing saved. Test in Task 1 + Task 6.
3. Legacy prod data: a ZIP submission in AWAITING_TEACHER_REVIEW with an attempt snapshotted as `quiz_then_teacher` completes when points are saved; a QUIZ_SENT legacy one passing routes by the *current* mode. Tests in Task 5 + Task 6.
4. "Перезапустити перевірки" on a QUIZ_ONLY submission would crash the worker (`saved_as` missing) — must be 409. Test in Task 6.
5. Teacher edits points of a COMPLETED submission — grade recomputed; clearing a required criterion refused. Test in Task 6.

---

### Task 1: Pure `teacher_scores` module

**Files:**
- Create: `src/submissions_checker/services/teacher_scores.py`
- Test: `tests/unit/test_teacher_scores.py`

**Interfaces:**
- Produces:
  - `MODE: str = "quiz_and_teacher_scores"`
  - `@dataclass(frozen=True) class Criterion: key: str; title: str; max: int; optional: bool`
  - `class ScoreError(ValueError)` — bad teacher input.
  - `def is_scored_mode(assignment_config: Mapping[str, Any] | None) -> bool`
  - `def criteria(grading_cfg: Mapping[str, Any] | None) -> list[Criterion]`
  - `def validate_assignment(code: str, a_cfg: Mapping[str, Any]) -> None` — raises `ValueError` with a readable message.
  - `def parse_form(crits: list[Criterion], form: Mapping[str, str]) -> dict[str, int]` — reads `score_<key>` fields; empty ⇒ omitted; raises `ScoreError`.
  - `def is_complete(crits: list[Criterion], scores: Mapping[str, int] | None) -> bool`
  - `def compute(grading_cfg, min_grade: int, max_grade: int, *, quiz_pct: float, scores: Mapping[str, int], round_up: bool = False) -> dict[str, Any]` — returns the `grade_breakdown` dict (spec §4) including `"grade"`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the quiz_and_teacher_scores grade rules (services.teacher_scores)."""

from __future__ import annotations

import pytest

from submissions_checker.services import teacher_scores as ts

GRADING = {
    "quiz_points": 8,
    "teacher_criteria": [
        {"key": "report", "title": "Звіт", "max": 5},
        {"key": "star", "title": "Зірочка", "max": 3, "optional": True},
    ],
}
A_CFG = {"review_mode": ts.MODE, "min_grade": 0, "max_grade": 16, "grading": GRADING,
         "quiz": {"questions": [{"text": "q"}]}}


def test_is_scored_mode() -> None:
    assert ts.is_scored_mode({"review_mode": ts.MODE})
    assert not ts.is_scored_mode({"review_mode": "quiz_then_teacher"})
    assert not ts.is_scored_mode(None)


def test_criteria_parsed_with_optional_default_false() -> None:
    crits = ts.criteria(GRADING)
    assert crits == [ts.Criterion("report", "Звіт", 5, False), ts.Criterion("star", "Зірочка", 3, True)]


def test_validate_accepts_matching_total() -> None:
    ts.validate_assignment("lab3", A_CFG)


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"quiz": {}}, "quiz"),
        ({"max_grade": 17}, "quiz_points + criteria"),
        ({"grading": {**GRADING, "teacher_criteria": []}}, "teacher_criteria"),
        ({"grading": {**GRADING, "quiz_points": -1}}, "quiz_points"),
        ({"grading": {**GRADING, "teacher_criteria": [{"key": "Bad Key", "title": "x", "max": 8}]}}, "key"),
        ({"grading": {**GRADING, "teacher_criteria": [{"key": "a", "title": "x", "max": 4},
                                                      {"key": "a", "title": "y", "max": 4}]}}, "duplicate"),
        ({"grading": {**GRADING, "teacher_criteria": [{"key": "a", "title": "x", "max": 0}]}}, "max"),
        ({"grading": {**GRADING, "teacher_criteria": [{"key": "a", "title": "x", "max": True}]}}, "max"),
    ],
)
def test_validate_rejects(patch: dict, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        ts.validate_assignment("lab3", {**A_CFG, **patch})


def test_parse_form_reads_ints_and_skips_empty() -> None:
    crits = ts.criteria(GRADING)
    assert ts.parse_form(crits, {"score_report": " 4 ", "score_star": ""}) == {"report": 4}


@pytest.mark.parametrize("value", ["abc", "-1", "6", "2.5"])
def test_parse_form_rejects_bad_values(value: str) -> None:
    with pytest.raises(ts.ScoreError):
        ts.parse_form(ts.criteria(GRADING), {"score_report": value})


def test_is_complete_needs_required_only() -> None:
    crits = ts.criteria(GRADING)
    assert ts.is_complete(crits, {"report": 0})
    assert not ts.is_complete(crits, {"star": 3})
    assert not ts.is_complete(crits, None)


def test_compute_sums_quiz_points_and_criteria() -> None:
    b = ts.compute(GRADING, 0, 16, quiz_pct=75.0, scores={"report": 4, "star": 2})
    assert b["grade"] == 12
    assert b["mode"] == ts.MODE
    assert b["quiz_score"] == 75.0
    assert b["quiz"] == {"pct": 75.0, "points": 6, "max": 8}
    assert b["criteria"] == [
        {"key": "report", "title": "Звіт", "points": 4, "max": 5},
        {"key": "star", "title": "Зірочка", "points": 2, "max": 3},
    ]


def test_compute_rounds_half_up_and_missing_optional_is_zero() -> None:
    # 56.25% of 8 = 4.5 -> 5 (half up; Python's round() would give 4)
    assert ts.compute(GRADING, 0, 16, quiz_pct=56.25, scores={"report": 5})["grade"] == 10
    b = ts.compute(GRADING, 0, 16, quiz_pct=56.25, scores={"report": 5})
    assert b["criteria"][1]["points"] == 0


def test_compute_round_up_for_squads_and_clamp() -> None:
    assert ts.compute(GRADING, 0, 16, quiz_pct=51.0, scores={"report": 0}, round_up=True)["quiz"]["points"] == 5
    assert ts.compute(GRADING, 0, 16, quiz_pct=100.0, scores={"report": 5, "star": 3})["grade"] == 16
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen --extra dev pytest tests/unit/test_teacher_scores.py -q`
Expected: FAIL — `ModuleNotFoundError: submissions_checker.services.teacher_scores`.

- [ ] **Step 3: Implement**

```python
"""Grade rules for ``review_mode: quiz_and_teacher_scores``.

The platform examines only the defence (a quiz); the teacher grades the work outside it and
types per-criterion points on the assignment board. The grade is quiz points plus teacher
points. Pure and DB-free: config validation, form parsing and the formula live here so
``config_apply``, ``grading`` and the teacher route share one definition.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MODE = "quiz_and_teacher_scores"
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    max: int
    optional: bool


class ScoreError(ValueError):
    """Teacher input that cannot be stored as points."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_scored_mode(assignment_config: Mapping[str, Any] | None) -> bool:
    return (assignment_config or {}).get("review_mode") == MODE


def criteria(grading_cfg: Mapping[str, Any] | None) -> list[Criterion]:
    out: list[Criterion] = []
    for raw in (grading_cfg or {}).get("teacher_criteria") or []:
        out.append(
            Criterion(
                key=str(raw["key"]),
                title=str(raw.get("title") or raw["key"]),
                max=int(raw["max"]),
                optional=bool(raw.get("optional", False)),
            )
        )
    return out


def validate_assignment(code: str, a_cfg: Mapping[str, Any]) -> None:
    where = f"assignment '{code}' ({MODE})"
    if not ((a_cfg.get("quiz") or {}).get("questions")):
        raise ValueError(f"{where} needs a quiz with questions")
    grading = a_cfg.get("grading") or {}
    quiz_points = grading.get("quiz_points")
    if not _is_int(quiz_points) or quiz_points < 0:
        raise ValueError(f"{where}: grading.quiz_points must be a non-negative integer")
    raw = grading.get("teacher_criteria")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where}: grading.teacher_criteria must list at least one criterion")
    seen: set[str] = set()
    total = 0
    for item in raw:
        key = (item or {}).get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(f"{where}: criterion key {key!r} must match [a-z0-9_]+")
        if key in seen:
            raise ValueError(f"{where}: duplicate criterion key '{key}'")
        seen.add(key)
        mx = item.get("max")
        if not _is_int(mx) or mx <= 0:
            raise ValueError(f"{where}: criterion '{key}' max must be a positive integer")
        total += mx
    span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    if quiz_points + total != span:
        raise ValueError(
            f"{where}: quiz_points + criteria max ({quiz_points} + {total}) must equal "
            f"max_grade - min_grade ({span})"
        )


def parse_form(crits: list[Criterion], form: Mapping[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in crits:
        raw = (form.get(f"score_{c.key}") or "").strip()
        if raw == "":
            continue
        if not raw.isdigit():
            raise ScoreError(f"{c.title}: '{raw}' is not a whole number")
        value = int(raw)
        if value > c.max:
            raise ScoreError(f"{c.title}: {value} is above the maximum {c.max}")
        out[c.key] = value
    return out


def is_complete(crits: list[Criterion], scores: Mapping[str, int] | None) -> bool:
    have = scores or {}
    return all(c.optional or c.key in have for c in crits)


def compute(
    grading_cfg: Mapping[str, Any] | None,
    min_grade: int,
    max_grade: int,
    *,
    quiz_pct: float,
    scores: Mapping[str, int],
    round_up: bool = False,
) -> dict[str, Any]:
    cfg = grading_cfg or {}
    quiz_max = int(cfg.get("quiz_points", 0))
    raw_quiz = quiz_pct / 100.0 * quiz_max
    if round_up:
        quiz_awarded = math.ceil(round(raw_quiz, 6))
    else:
        quiz_awarded = int(Decimal(str(round(raw_quiz, 6))).quantize(Decimal(1), ROUND_HALF_UP))
    rows = [
        {"key": c.key, "title": c.title, "points": int(scores.get(c.key, 0)), "max": c.max}
        for c in criteria(cfg)
    ]
    total = min_grade + quiz_awarded + sum(r["points"] for r in rows)
    return {
        "grade": max(min_grade, min(max_grade, total)),
        "mode": MODE,
        "quiz_score": quiz_pct,
        "quiz": {"pct": quiz_pct, "points": quiz_awarded, "max": quiz_max},
        "criteria": rows,
    }
```

Note the test comment: 56.25% of 8 = 4.5 → 5 (half up) + report 5 = 10.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --frozen --extra dev pytest tests/unit/test_teacher_scores.py -q` → all PASS. Then `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format src/submissions_checker/services/teacher_scores.py tests/unit/test_teacher_scores.py`.

- [ ] **Step 5: Commit** — `Add grade rules for quiz plus teacher-entered points` (body: why a pure module shared by config apply, grading and the board).

---

### Task 2: Accept the mode in Apply config

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py:45-55` (`_QUIZ_REACHABLE_MODES`), `:~131` (validator call list), new `_validate_teacher_scores`
- Modify: `src/submissions_checker/workers/tasks/check_tasks.py:63` (`_QUIZ_FIRST_MODES`)
- Test: `tests/unit/test_config_apply_helpers.py`, `tests/functional/test_apply_config.py`

**Interfaces:**
- Consumes: `teacher_scores.MODE`, `teacher_scores.validate_assignment`.

- [ ] **Step 1: Failing tests.** Unit (in `test_config_apply_helpers.py`, uses the existing `svc` fixture):

```python
def test_validate_teacher_scores_rejects_bad_total(svc: ConfigApplyService) -> None:
    cfg = {"assignments": {"lab1": {
        "review_mode": "quiz_and_teacher_scores", "max_grade": 14,
        "grading": {"quiz_points": 8, "teacher_criteria": [{"key": "report", "title": "Звіт", "max": 5}]},
        "quiz": {"questions": [{"text": "q"}]}}}}
    with pytest.raises(ValueError, match="lab1"):
        svc._validate_teacher_scores(cfg)


def test_validate_teacher_scores_ignores_other_modes(svc: ConfigApplyService) -> None:
    svc._validate_teacher_scores({"assignments": {"lab1": {"review_mode": "quiz_then_teacher"}}})


def test_scored_mode_is_quiz_reachable_and_quiz_first() -> None:
    from submissions_checker.services.config_apply import _QUIZ_REACHABLE_MODES
    from submissions_checker.workers.tasks.check_tasks import _QUIZ_FIRST_MODES
    assert "quiz_and_teacher_scores" in _QUIZ_REACHABLE_MODES
    assert "quiz_and_teacher_scores" in _QUIZ_FIRST_MODES
```

Functional (in `test_apply_config.py`, follow the file's existing helper for building/uploading a config ZIP): applying a config whose `lab1` uses the mode with matching totals succeeds and `SubjectsAssignment.config["grading"]["teacher_criteria"]` is stored; a mismatching total returns the page's error (same assertion style as the file's existing "rejects" tests).

- [ ] **Step 2:** run → FAIL (`AttributeError: _validate_teacher_scores`).
- [ ] **Step 3: Implement.** Add `"quiz_and_teacher_scores"` to both frozensets (comment in `check_tasks`: "no sandbox ever runs; usually opened without an upload"). In `config_apply.py`:

```python
    def _validate_teacher_scores(self, new_cfg: dict[str, Any]) -> None:
        """Reject a quiz_and_teacher_scores assignment whose points cannot add up."""
        for code, a_cfg in (new_cfg.get("assignments") or {}).items():
            if teacher_scores.is_scored_mode(a_cfg):
                teacher_scores.validate_assignment(code, a_cfg or {})
```

and call `self._validate_teacher_scores(new_cfg)` right after `self._validate_quiz_reachability(new_cfg)`. Import `from submissions_checker.services import teacher_scores`.
- [ ] **Step 4:** run both test files → PASS.
- [ ] **Step 5: Commit** — `Accept quiz_and_teacher_scores in Apply config`.

---

### Task 3: Schema — `QUIZ_ONLY`, `teacher_scores`, `quiz_opened`

**Files:**
- Create: `alembic/versions/0034_quiz_and_teacher_scores.py`
- Modify: `src/submissions_checker/db/models/enums.py:24` (add `QUIZ_ONLY = "QUIZ_ONLY"`)
- Modify: `src/submissions_checker/db/models/student_assignment.py` (column)
- Modify: `src/submissions_checker/core/state_machine.py` (PENDING gets `"quiz_opened": SubmissionStatus.QUIZ_SENT`)
- Modify: `CLAUDE.md` alembic line (0001…0034; 0034 = QUIZ_ONLY + teacher_scores, additive) — local-only file
- Test: `tests/unit/test_state_machine.py` (or the existing file that tests transitions — `grep -l "start_validation" tests/unit`), functional round-trip

- [ ] **Step 1: Failing tests.**

```python
def test_quiz_opened_moves_pending_to_quiz_sent() -> None:
    sub = SimpleNamespace(status=SubmissionStatus.PENDING)
    transition(sub, "quiz_opened")
    assert sub.status == SubmissionStatus.QUIZ_SENT
```

Functional (`tests/functional/test_quiz_and_teacher_scores.py`, new file, module docstring "quiz_and_teacher_scores: upload-less quiz + teacher points"): create a `Submission(source_type=SubmissionSourceType.QUIZ_ONLY, source_metadata={}, status=SubmissionStatus.QUIZ_SENT, ...)` and set `sa.teacher_scores = {"report": 4}`, commit, refresh, assert both round-trip. (The functional schema is built by running migrations — `_schema_ready` in `tests/functional/conftest.py` — so this proves the migration.)

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.** Migration:

```python
"""quiz_and_teacher_scores: upload-less submissions and teacher-entered points.

Purely additive — a new enum value and a nullable column — so a replica still on the
previous release keeps working until it is replaced. The enum value cannot be dropped on
downgrade (PostgreSQL has no DROP VALUE); it is harmless when unused.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE submission_source_type ADD VALUE IF NOT EXISTS 'QUIZ_ONLY'")
    op.add_column(
        "students_assignments",
        sa.Column("teacher_scores", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("students_assignments", "teacher_scores")
```

Model: `teacher_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)` (import `JSONB` from `sqlalchemy.dialects.postgresql`, `Any` from typing, matching `submission.py`). State machine: add `"quiz_opened": SubmissionStatus.QUIZ_SENT,` under PENDING with a comment "quiz_and_teacher_scores: the student opened the quiz with nothing to check".
- [ ] **Step 4:** run unit + new functional test, plus `tests/unit/test_migrations.py tests/unit/test_models.py` → PASS. Also on the dev DB: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/submissions_checker uv run --frozen alembic upgrade head` then `alembic downgrade -1` then `upgrade head` (if the dev stack is up).
- [ ] **Step 5: Commit** — `Add QUIZ_ONLY source, teacher_scores column, quiz_opened edge`.

---

### Task 4: `finalize_grade` branch

**Files:**
- Modify: `src/submissions_checker/services/grading.py:132-207`
- Test: `tests/functional/test_quiz_and_teacher_scores.py`

**Interfaces:**
- Consumes: `teacher_scores.is_scored_mode/criteria/is_complete/compute`.
- Produces: `finalize_grade(db, submission) -> dict[str, Any] | None` (return type widened to the persisted breakdown dict; no caller uses the value — verified by grep). Returns None and writes nothing in scored mode when quiz not passed or required points missing.

- [ ] **Step 1: Failing tests** (add an `_arrange_scored(db, teacher, make_student, *, status, scores=None)` helper to the new test file modelled on `_arrange` in `tests/functional/test_teacher_grant_quiz_attempt.py`, but: assignment `config={"review_mode": "quiz_and_teacher_scores", "grading": GRADING}` with `max_grade=16`, plugin config `{"assignments": {"l1": {"review_mode": "quiz_and_teacher_scores", "grading": GRADING, "quiz": QUIZ}}}`, submission `source_type=QUIZ_ONLY`, `source_metadata={}`; and `_attempt(...)` copied from that file):

```python
async def test_finalize_scored_writes_sum_and_breakdown(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(db, teacher, make_student,
                                        status=SubmissionStatus.COMPLETED, scores={"report": 4, "star": 2})
    await _attempt(db, sub, sa.student_id, is_passed=True, score=3, max_score=4)  # 75%
    await finalize_grade(db, sub)
    await db.commit()
    await db.refresh(sa); await db.refresh(sub)
    assert sa.grade == 12
    assert sub.grade_breakdown["quiz"] == {"pct": 75.0, "points": 6, "max": 8}


async def test_finalize_scored_noop_without_required_points(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(db, teacher, make_student,
                                        status=SubmissionStatus.COMPLETED, scores={"star": 2})
    await _attempt(db, sub, sa.student_id, is_passed=True, score=4, max_score=4)
    assert await finalize_grade(db, sub) is None
    await db.refresh(sa)
    assert sa.grade is None


async def test_finalize_scored_noop_without_passed_quiz(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(db, teacher, make_student,
                                        status=SubmissionStatus.COMPLETED, scores={"report": 5})
    assert await finalize_grade(db, sub) is None
```

(`_attempt` gains `score`/`max_score` keyword params.)

- [ ] **Step 2:** run → FAIL (grade computed by the blend path).
- [ ] **Step 3: Implement.** In `finalize_grade`, after `quiz_pct`/squad are resolved and before `compute_grade(...)`:

```python
    if teacher_scores.is_scored_mode(subjects_assignment.config):
        crits = teacher_scores.criteria(grading_cfg)
        scores = sa.teacher_scores or {}
        if quiz_pct is None or not teacher_scores.is_complete(crits, scores):
            logger.info("finalize_grade_scored_incomplete", submission_id=submission.id)
            return None
        breakdown_dict = teacher_scores.compute(
            grading_cfg, subjects_assignment.min_grade, subjects_assignment.max_grade,
            quiz_pct=quiz_pct, scores=scores, round_up=squad is not None,
        )
        grade = int(breakdown_dict["grade"])
    else:
        breakdown = compute_grade(...)   # existing call, unchanged
        grade = breakdown.grade
        breakdown_dict = breakdown.to_dict()
```

then use `grade` when writing rows, keep the squad annotation, set `sub.grade_breakdown = breakdown_dict`, log `grade=grade`, and `return breakdown_dict`. Update the docstring ("…or the teacher's points under quiz_and_teacher_scores").
- [ ] **Step 4:** run new file + `tests/unit/test_grading.py tests/functional/test_student_quiz_grading_branches.py` → PASS.
- [ ] **Step 5: Commit** — `Grade quiz_and_teacher_scores as quiz points plus teacher points`.

---

### Task 5: Student opens the quiz without an upload; quiz outcome routing

**Files:**
- Create: `src/submissions_checker/services/quiz_open.py`
- Modify: `src/submissions_checker/api/routes/student_quiz.py:721-750` (start route) and `:648-657` (pass branch)
- Modify: `src/submissions_checker/services/quiz_regrade.py:146-181`
- Modify: `src/submissions_checker/api/routes/student_portal.py` submit route (refuse upload in this mode)
- Test: `tests/functional/test_quiz_and_teacher_scores.py`

**Interfaces:**
- Produces:
  - `class QuizOpenError(Exception)` (message = HTTP detail)
  - `async def open_quiz_submission(db, sa: StudentAssignment, student_id: int) -> Submission` — locks `sa` row, returns the existing latest (squad-resolved) submission or creates `QUIZ_ONLY` + `quiz_opened`; never commits.
  - `async def scored_quiz_outcome(db, submission) -> str` in `teacher_scores`? **No** — keep DB code out of the pure module: add `async def scores_complete_for(db: AsyncSession, submission: Submission) -> bool` to `quiz_open.py` (reads the submission's SA `teacher_scores` and current assignment config).

- [ ] **Step 1: Failing tests** (HTTP, using `make_user(role=STUDENT, student=...)` + `authenticate` as in `_granted` of the grant test file; arrange the scored assignment with **no** submission):

```python
async def test_student_opens_quiz_without_upload(client, db, teacher, make_user, make_student) -> None:
    subject, asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    user = await make_user(role=UserRole.STUDENT, username="val", student=student)
    authenticate(client, user)
    r = await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/quiz/")
    subs = (await db.execute(select(Submission).where(Submission.students_assignment_id == sa.id))).scalars().all()
    assert len(subs) == 1
    assert subs[0].source_type == SubmissionSourceType.QUIZ_ONLY
    assert subs[0].status == SubmissionStatus.QUIZ_SENT
    assert subs[0].plugin_config_id is not None


async def test_second_open_reuses_the_submission(client, db, teacher, make_user, make_student) -> None:
    # two GETs (second one resumes the IN_PROGRESS attempt) -> still exactly one submission
    ...same arrange...
    for _ in range(2):
        await client.get(url, follow_redirects=False)
    assert count_of_submissions == 1


async def test_open_refused_for_other_modes(client, db, teacher, make_user, make_student) -> None:
    # assignment config review_mode quiz_then_teacher, no submission -> 403 as today
    ...
    assert r.status_code == 403


async def test_pass_without_points_goes_to_teacher(client, db, teacher, make_user, make_student) -> None:
    # open, answer all correctly via POST /portal/quiz/{id}/submit (form as in the grant test)
    # -> submission AWAITING_TEACHER_REVIEW, sa.grade None


async def test_pass_with_points_completes(client, db, teacher, make_user, make_student) -> None:
    # sa.teacher_scores={"report": 5} before opening; pass -> COMPLETED, sa.grade == 8 + 5


async def test_legacy_quiz_then_teacher_attempt_routes_by_current_mode(client, db, teacher, make_user, make_student) -> None:
    # ZIP_UPLOAD submission in QUIZ_SENT; assignment config now scored; scores complete;
    # attempt started via the route, then its config_snapshot["review_mode"] overwritten to
    # "quiz_then_teacher" before submit -> passes -> COMPLETED with the scored grade


async def test_upload_refused_in_scored_mode(client, db, teacher, make_user, make_student) -> None:
    files = {"file": ("w.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")}
    r = await client.post(f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit", files=files)
    assert r.status_code == 409
```

Write each `...` body out fully when implementing (arrange helper `_arrange_scored_no_sub` = `_arrange_scored` without creating the submission; correct answer index is `1` for the shared `QUIZ`; the quiz needs `recording_consent_at` set, as in `_arrange`). Check the exact submit URL with `grep -n '@router.post' src/submissions_checker/api/routes/student_portal.py`.

Also a dispute-regrade test: scored assignment, FAILED submission with a failed attempt that a regrade flips to passing, points complete → `_advance_submission` lands COMPLETED (call `quiz_regrade._advance_submission(db, attempt)` directly after setting `attempt.is_passed=True`).

- [ ] **Step 2:** run → FAIL (403 "Quiz not available").
- [ ] **Step 3: Implement.**

`services/quiz_open.py`:

```python
"""Open a quiz with nothing uploaded (review_mode quiz_and_teacher_scores).

The quiz machinery hangs off a Submission; in this mode the student never uploads, so the
first time they open the quiz an upload-less QUIZ_ONLY submission is created and moved
straight to QUIZ_SENT. An existing submission (an old ZIP on prod, a squad-mate's) is reused.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import StudentAssignment, Subject, SubjectsAssignment, Submission
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads, teacher_scores


class QuizOpenError(Exception):
    """The quiz cannot be opened from here (message is the HTTP detail)."""


async def open_quiz_submission(
    db: AsyncSession, sa: StudentAssignment, student_id: int
) -> Submission:
    # Serialise concurrent opens of the same assignment by the same student.
    await db.execute(
        select(StudentAssignment.id).where(StudentAssignment.id == sa.id).with_for_update()
    )
    existing = await squads.latest_submission(db, sa)
    if existing is not None:
        return existing
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None or not teacher_scores.is_scored_mode(asg.config):
        raise QuizOpenError("Quiz not available for this submission")
    subject = await db.get(Subject, asg.subject_id)
    squad = None
    if subject is not None and subject.squad_max_size is not None:
        if await squads.has_pending_invites(db, asg.subject_id, student_id) or (
            await squads.squad_has_pending_invites(db, asg.subject_id, student_id)
        ):
            raise QuizOpenError("Squad invitation pending; answer or cancel it first.")
        squad = await squads.squad_of(db, asg.subject_id, student_id)
    config_id = await db.scalar(
        select(SubjectPluginConfig.id)
        .where(SubjectPluginConfig.subject_id == asg.subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    if config_id is None:
        raise QuizOpenError("No plugin config for this subject")
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=SubmissionStatus.PENDING,
        plugin_config_id=config_id,
        squad_id=squad.id if squad else None,
        test_results={"skipped": True, "reason": teacher_scores.MODE},
    )
    db.add(sub)
    await db.flush()
    if squad:
        squads.lock_on_submit(squad)
    transition(sub, "quiz_opened")
    return sub


async def scores_complete_for(db: AsyncSession, submission: Submission) -> bool:
    sa = await db.get(StudentAssignment, submission.students_assignment_id)
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id) if sa else None
    if sa is None or asg is None:
        return False
    crits = teacher_scores.criteria((asg.config or {}).get("grading"))
    return teacher_scores.is_complete(crits, sa.teacher_scores)


async def is_scored_submission(db: AsyncSession, submission: Submission) -> bool:
    sa = await db.get(StudentAssignment, submission.students_assignment_id)
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id) if sa else None
    return asg is not None and teacher_scores.is_scored_mode(asg.config)
```

Start route (`student_quiz.py`), replacing the `latest_sub is None` gate:

```python
    latest_sub = await squads.latest_submission(db, sa)
    if latest_sub is None and teacher_scores.is_scored_mode(sa.subjects_assignment.config):
        try:
            latest_sub = await quiz_open.open_quiz_submission(db, sa, student_id)
        except quiz_open.QuizOpenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info("quiz_submission_opened", submission_id=latest_sub.id, student_id=student_id,
                    assignment_id=sa.subjects_assignment_id)
    if latest_sub is None or latest_sub.status != SubmissionStatus.QUIZ_SENT:
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")
```

(The route already commits after creating the attempt — confirm with a read of the route's tail; the new submission rides the same commit.)

Pass branch (`student_quiz.py:652`), replace the snapshot-only test:

```python
                if await quiz_open.is_scored_submission(db, submission):
                    # Teacher points may already be in; the quiz was the missing half.
                    if await quiz_open.scores_complete_for(db, submission):
                        transition(submission, "quiz_passed")
                        await finalize_grade(db, submission)
                    else:
                        transition(submission, "quiz_passed_teacher")
                        await enqueue_teacher_review_notification(db, submission.id)
                elif attempt.config_snapshot.get("review_mode") == "quiz_then_teacher":
                    ...unchanged...
```

`quiz_regrade._advance_submission`: compute `scored = await quiz_open.is_scored_submission(db, submission)`; when scored, `to_teacher = not await quiz_open.scores_complete_for(db, submission)`; otherwise keep the snapshot rule. (Watch the import cycle: `quiz_open` imports nothing from `quiz_regrade`.)

Submit route (`student_portal.py`), right after loading `subjects_assignment`:

```python
    if teacher_scores.is_scored_mode(subjects_assignment.config):
        raise HTTPException(status_code=409, detail="This assignment takes no upload; open the quiz.")
```

- [ ] **Step 4:** run new file + `tests/functional/test_student_quiz*.py tests/functional/test_quiz_disputes.py tests/functional/test_squad_quiz.py tests/functional/test_quiz_first_and_stepper.py` → PASS.
- [ ] **Step 5: Commit** — `Open the quiz without an upload under quiz_and_teacher_scores`.

---

### Task 6: Teacher saves points; approve/rerun guards

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` — new route after `teacher_grant_quiz_attempt`; `_apply_review_decision` (~1441); single review POST (~1514) and bulk (~1548); `teacher_rerun_checks` (~1682)
- Test: `tests/functional/test_quiz_and_teacher_scores.py`

**Interfaces:**
- Consumes: `teacher_scores.criteria/parse_form/is_complete/ScoreError`, `quiz_open.is_scored_submission`, `squads.squad_of/member_sa_ids/latest_submission`.
- Produces: `POST /teacher/subjects/{subject_id}/assignments/{sa_id}/scores` form `student_id`, `score_<key>…` → 303 to the board (`?scores=saved` / `?scores_error=<msg>` flash), 404/403/409/422 on guards. `class ScoresIncompleteError(Exception)` in `teacher_portal.py`.

- [ ] **Step 1: Failing tests:**

```python
async def test_teacher_saves_points_before_quiz(...)        # no submission -> sa.teacher_scores stored, grade None, audit teacher_scores_set
async def test_saving_points_completes_waiting_submission(...)  # AWAITING_TEACHER_REVIEW + passed attempt -> COMPLETED, grade, SUBMISSION_REVIEWED outbox row
async def test_legacy_zip_submission_completes_on_points(...)   # ZIP_UPLOAD + attempt snapshot quiz_then_teacher, AWAITING_TEACHER_REVIEW -> COMPLETED
async def test_editing_points_after_completion_regrades(...)    # COMPLETED, report 4 -> 5 => grade +1
async def test_clearing_required_after_completion_refused(...)  # 422, scores unchanged
@pytest.mark.parametrize("value", ["abc", "-1", "6"])
async def test_bad_value_rejected(...)                           # 422, nothing saved
async def test_scores_route_other_teacher_403(...)
async def test_scores_route_not_scored_mode_409(...)
async def test_review_approve_refused_without_points(...)        # POST /teacher/submissions/{id}/review action=approve -> 409; status unchanged
async def test_bulk_approve_skips_without_points(...)            # redirect ?bulk=0,1
async def test_rerun_refused_for_quiz_only(...)                  # FAILED QUIZ_ONLY -> 409
```

Write each body fully (arrange via `_arrange_scored`; teacher via `authenticate(client, teacher)`; form `{"student_id": str(student.id), "score_report": "4", "score_star": "2"}`; check the review/bulk URLs and form names with `grep -n '@router.post' src/submissions_checker/api/routes/teacher_portal.py` and the existing `test_teacher_portal.py` approve tests).

Decision for errors: the board is a plain HTML form, so 422 is returned as a redirect? **No** — return `HTTPException(422, detail=msg)`; tests assert 422. (Keeps parity with other teacher guards; a nicer flash is out of scope.)

- [ ] **Step 2:** run → FAIL (404 route).
- [ ] **Step 3: Implement.**

```python
@router.post("/subjects/{subject_id}/assignments/{sa_id}/scores")
async def teacher_save_scores(
    request: Request, subject_id: int, sa_id: int, db: DBSession, current_user: TeacherUser,
) -> RedirectResponse:
    """Store a student's per-criterion points (quiz_and_teacher_scores) and grade if possible."""
    await require_subject_access(db, subject_id, current_user)
    assignment = await db.get(SubjectsAssignment, sa_id)
    if assignment is None or assignment.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not teacher_scores.is_scored_mode(assignment.config):
        raise HTTPException(status_code=409, detail="Assignment does not take teacher points")
    form = await request.form()
    try:
        student_id = int(str(form.get("student_id", "")))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="student_id required") from exc
    enrolled = await db.scalar(select(SubjectsStudents.student_id).where(
        SubjectsStudents.subject_id == subject_id, SubjectsStudents.student_id == student_id))
    if enrolled is None:
        raise HTTPException(status_code=404, detail="Student not enrolled")
    crits = teacher_scores.criteria((assignment.config or {}).get("grading"))
    try:
        new_scores = teacher_scores.parse_form(crits, {k: str(v) for k, v in form.items()})
    except teacher_scores.ScoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    own = await db.scalar(select(StudentAssignment).where(
        StudentAssignment.student_id == student_id,
        StudentAssignment.subjects_assignment_id == sa_id).with_for_update())
    if own is None:
        own = StudentAssignment(student_id=student_id, subjects_assignment_id=sa_id)
        db.add(own)
        await db.flush()
    submission = await squads.latest_submission(db, own)
    if (submission is not None and submission.status == SubmissionStatus.COMPLETED
            and not teacher_scores.is_complete(crits, new_scores)):
        raise HTTPException(status_code=422, detail="A graded work cannot lose a required score")

    squad = await squads.squad_of(db, subject_id, student_id)
    target_ids = await squads.member_sa_ids(db, squad, sa_id) if squad else [own.id]
    old = dict(own.teacher_scores or {})
    for row_id in target_ids:
        row = await db.get(StudentAssignment, row_id)
        if row is not None:
            row.teacher_scores = dict(new_scores)
    await audit(db, action="teacher_scores_set", actor_id=current_user.user_id,
                actor_username=current_user.username, target_type="student_assignment",
                target_id=own.id, student_id=student_id, old=old, new=new_scores)

    if submission is not None and teacher_scores.is_complete(crits, new_scores):
        await db.flush()
        if submission.status == SubmissionStatus.AWAITING_TEACHER_REVIEW and await _quiz_passed(db, submission):
            transition(submission, "teacher_approve")
            await finalize_grade(db, submission)
            db.add(OutboxMessage(event_type=OutboxEventType.SUBMISSION_REVIEWED,
                                 state=OutboxMessageState.PENDING,
                                 payload={"submission_id": submission.id, "action": "approve", "reason": ""}))
        elif submission.status == SubmissionStatus.COMPLETED:
            await finalize_grade(db, submission)
    await db.commit()
    return RedirectResponse(url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}", status_code=303)
```

Add `_quiz_passed(db, submission) -> bool` (the `select(QuizAttempt.id)… is_passed … limit(1)` query already inlined in `_apply_review_decision`; extract it and reuse in both places). Check `squads.squad_of` signature with `grep -n "async def squad_of" src/submissions_checker/services/squads.py` before use.

`_apply_review_decision`, approve branch, first lines:

```python
        if teacher_scores.is_scored_mode(subjects_assignment.config):
            crits = teacher_scores.criteria((subjects_assignment.config or {}).get("grading"))
            if not teacher_scores.is_complete(crits, submission.students_assignment.teacher_scores):
                raise ScoresIncompleteError
```

Single review POST: `except ScoresIncompleteError: raise HTTPException(409, "Enter the work points on the assignment board first")`. Bulk: add `ScoresIncompleteError` to the `except InvalidTransitionError` that counts `skipped`.

`teacher_rerun_checks`: before `_requeue_checks`:

```python
    if submission.source_type == SubmissionSourceType.QUIZ_ONLY:
        raise HTTPException(status_code=409, detail="Nothing was uploaded; there is nothing to re-check")
```

- [ ] **Step 4:** run new file + `tests/functional/test_teacher_portal*.py tests/functional/test_teacher_grant_quiz_attempt.py` → PASS.
- [ ] **Step 5: Commit** — `Let teachers enter per-criterion points on the board`.

---

### Task 7: UI — board columns, student panel, gradebook tooltip, i18n

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_assignment` context: `scored`, `criteria`, per-row `teacher_scores`, `quiz_cells`)
- Modify: `templates/teacher_assignment.html` (header ~113-125, row ~128-247)
- Modify: `src/submissions_checker/api/routes/student_portal.py` `assignment_detail` (cap from latest config when no submission; always pass breakdown in scored mode)
- Modify: `templates/assignment_detail.html` (quiz panel ~209-290, breakdown ~143-166, upload form ~296-330)
- Modify: `src/submissions_checker/services/gradebook.py` + `templates/teacher_subject.html:136` (cell `title`)
- Modify: `i18n/uk.yml`
- Test: `tests/functional/test_quiz_and_teacher_scores.py`

- [ ] **Step 1: Failing tests** (HTML assertions):

```python
async def test_board_shows_criteria_inputs_and_total(...):
    # scored, one student with scores {"report": 4, "star": 2} and a passed 75% attempt, COMPLETED, grade 12
    page = await client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")
    assert 'name="score_report"' in page.text and 'value="4"' in page.text
    assert "/5" in page.text and "/3" in page.text
    assert "6/8" in page.text          # quiz cell
    assert "12" in page.text
    assert f'/teacher/subjects/{subject.id}/assignments/{asg.id}/scores' in page.text

async def test_board_has_no_score_inputs_in_other_modes(...):
    assert 'name="score_' not in page.text

async def test_student_page_offers_quiz_without_upload(...):
    # scored, no submission
    page = await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")
    assert f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz" in page.text
    assert 'id="upload-form"' not in page.text
    assert "0/2" in page.text         # attempts used / cap from the latest config

async def test_student_page_shows_breakdown_when_completed(...):
    assert "Тест 6/8" in page.text and "Звіт 4/5" in page.text

async def test_gradebook_cell_title_has_breakdown(...):
    page = await client.get(f"/teacher/subjects/{subject.id}?tab=students")  # check the real tab URL in test_gradebook.py
    assert "Тест 6/8 · Звіт 4/5 · Зірочка 2/3" in page.text
```

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.**

i18n (`i18n/uk.yml`, `teacher:` section): `col_quiz: Тест`, `col_total: Разом`, `save_scores: Зберегти`, `quiz_not_passed: не складено`; `quiz:` section: `start_quiz: Почати тест`, `scored_waiting: "Тест складено: {points}/{max}. Очікуємо бали викладача за роботу."`, `scored_exhausted: Спроби вичерпано — зверніться до викладача.`, `breakdown_quiz: Тест`. Put the breakdown formatting in one Jinja macro `scored_breakdown(gb)` in a new partial `templates/_scored_breakdown.html` rendering `Тест {{gb.quiz.points}}/{{gb.quiz.max}} · {% for c in gb.criteria %}{{c.title}} {{c.points}}/{{c.max}}{% if not loop.last %} · {% endif %}{% endfor %}`, used by all three pages; gradebook builds the same string in Python (`gradebook.breakdown_line(bd) -> str | None`) for the `title` attribute.

Board context: `scored = teacher_scores.is_scored_mode(assignment.config)`; when scored, add `teacher_scores` to the row select (`StudentAssignment.teacher_scores`) and build `quiz_cells: dict[int, dict]` keyed by `student_id`: query `QuizAttempt` rows for the rows' `submission_id`s, per student pick the passed one (pct, points via `teacher_scores.compute(...)["quiz"]` or the row's `grade_breakdown["quiz"]` when present), else `used/cap`. Template: when `scored`, insert `<th>` for `col_quiz`, one per criterion (`{{ c.title }} /{{ c.max }}`), `col_total`, and an empty one; each row gets `<form id="scores-{{ row.student_id }}" method="POST" action="…/scores">` in the last cell with the hidden `student_id` and the submit button, and `<input form="scores-{{ row.student_id }}" name="score_{{ c.key }}" type="number" min="0" max="{{ c.max }}" value="…" class="w-14 …">` in each criterion cell (inputs outside the `<form>` element are tied by the `form` attribute — a `<form>` may not wrap a `<tr>`).

Student detail: when scored and `latest_sub is None`, read `max_quiz_attempts` from the latest `SubjectPluginConfig` (`assignments[code].quiz.max_quiz_attempts`); pass `scored=True` to the template; breakdown shown whenever scored (ignore `show_breakdown_to_student`). Template: when `scored`, hide the upload card entirely; quiz panel states per spec §2 (button when `submission_status in (None, "QUIZ_SENT")` and attempts left; waiting text when `AWAITING_TEACHER_REVIEW`; exhausted text when `FAILED`; grade + `scored_breakdown` when `COMPLETED`).

- [ ] **Step 4:** run new file + `tests/functional/test_gradebook.py tests/functional/test_portal_detail_pages.py tests/functional/test_i18n.py tests/unit/test_i18n.py` → PASS. Then start the dev stack (`make up`, check the compose working dir per CLAUDE.md) and look at both pages with a scored subject (apply a test config ZIP) — use the `run` skill.
- [ ] **Step 5: Commit** — `Show teacher points and the quiz half on the board and student page`.

---

### Task 8: Docs

**Files:** `docs/PLUGIN_AUTHORING.md` (review-modes table + new "quiz_and_teacher_scores" subsection with the YAML from spec §1 and the validation rules), `docs/feature_catalog.md` (new scores route; quiz route now may create a QUIZ_ONLY submission; state diagram gets `quiz_opened`), `docs/teacher_journey_guide.md` + `docs/student_journey_guide.md` (one paragraph each), `CLAUDE.md` (local-only: review modes list, migration 0034, `services/teacher_scores.py`, `services/quiz_open.py`).

- [ ] **Step 1:** edit; **Step 2:** commit `Document the quiz_and_teacher_scores mode`.

---

### Task 9: Subject config (`/home/vampir/petProjects/basicsOfTheDistributedSoftwareDevelopment`)

**Files:** `config.yml` (header comment lines 1–25; each `labN` block), `README.md` "Оцінювання", `scripts/validate_config.py` (only if it whitelists review modes — `grep -n review_mode scripts/validate_config.py`).

- [ ] **Step 1:** for each lab set `review_mode: quiz_and_teacher_scores`, `max_grade` = 13/13/16/15/15/15/13, replace `grading` with:

```yaml
    grading:
      quiz_points: 8                # захист
      teacher_criteria:
        - { key: report, title: "Звіт", max: 5 }
        - { key: star, title: "Завдання з зірочкою", max: 3, optional: true }   # лише lab3 (3), lab4–6 (max: 2)
```

Remove `max_submissions` (no uploads). Rewrite the header: the student opens the quiz any time; the teacher enters report/star points on the board; grade = quiz share of 8 + points.
- [ ] **Step 2:** `python scripts/validate_config.py` (or the repo's documented command) → passes; then dry-run the platform validator: `uv run --frozen python -c "import yaml; from submissions_checker.services import teacher_scores as t; c=yaml.safe_load(open('<repo>/config.yml')); [t.validate_assignment(k, v) for k, v in c['assignments'].items()]"` from the platform repo → no error.
- [ ] **Step 3:** commit in that repo (`Open the defence quiz without an upload`, body: why). **Do not push** — pushing triggers the release; it is part of the rollout (Task 10) and needs the owner's go.

---

### Task 10: Verify, then roll out (owner-confirmed steps)

- [ ] Full suite `uv run --frozen --extra dev pytest -q`, ruff check + format check, mypy, `make e2e` — all green, read the output.
- [ ] `superpowers:requesting-code-review` on the branch; fix findings.
- [ ] Merge per `superpowers:finishing-a-development-branch` (owner chooses).
- [ ] Rollout (each step only with the owner's go): `scripts/ops/run-prod-backup.sh --now` → push main (CI builds, Watchtower deploys both replicas; migration 0034 runs at startup) → confirm `/version` on prod and `select column_name from information_schema.columns where table_name='students_assignments' and column_name='teacher_scores'` via `connect-to-prod-db` → push the subject repo (release builds the ZIP) → Apply config in the teacher UI → read-only check: `select code, max_grade, config->>'review_mode' from subjects_assignments where subject_id=1 order by id` and that the 21 AWAITING_TEACHER_REVIEW rows are unchanged.
