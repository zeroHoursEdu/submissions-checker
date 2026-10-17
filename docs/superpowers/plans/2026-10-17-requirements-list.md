# Requirements List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `review_mode` pipelines with a per-assignment list of independent requirements whose sum is the grade; add batch scoring with a locked acceptance check; give the LLM grader the student's GitHub code.

**Architecture:** A pure module `services/requirements.py` parses/validates/translates configs into a `Plan` and evaluates `Facts` into a grade. A DB module `services/settle.py` gathers facts for a student's assignment (squad-aware) and sets grade + submission status after every event; all mode-specific branches in workers and routes are replaced by one `settle()` call. GitHub code is fetched in memory by `services/llm_grading/github_code.py` and attached to the judge request.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Postgres, Jinja, httpx, pytest + testcontainers, new dependency `pypdf`.

**Spec:** `docs/superpowers/specs/2026-10-17-requirements-list-design.md`

## Global Constraints

- Always `uv run --frozen …`; suite: `uv run --frozen --extra dev pytest -q`; lint: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/`; types: `uv run --frozen mypy src/`. Repo is clean at HEAD — any failure is ours.
- Transitions only through `state_machine.transition()`; never assign `submission.status`.
- UI text Ukrainian, via `vocab.*` (`i18n/uk.yml`).
- No schema migration. `students_assignments.teacher_scores` stays `dict[str, int]`; a saved check is `1`.
- `Σ points/max == max_grade - min_grade` for new configs; grade = `min_grade + Σ earned`, rounded once (squads: `ceil(round(x, 6))`; new configs half-up; translated weighted modes banker's `round()` as the old `compute_grade`).
- GitHub: no token, 20 s timeout, abort download past 20 MB, keep ≤200 files / ≤1 MB, extensions `.cpp .cc .c .h .hpp .rc .txt` + `CMakeLists.txt` `.sln` `.vcxproj`, skip dirs `.git/ x64/ Debug/ Release/ .vs/ build/`. Code never written to disk, MinIO or DB.
- Students never see AI drafts or unsaved teacher scores.
- Commits: imperative subject ≤72 chars, body explains why, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Squad mates passing the quiz at the same instant → exactly one transition, same grade on both rows (settle locks the latest submission row; `requirements_met` is allowed from `COMPLETED`). Test in Task 5.
2. A stale board tab posting a cleared checkbox for an already-accepted student → the check stays `1`, no error. Test in Task 9.
3. Batch save where one row's draft changed since render → that row skipped with reason, other rows saved. Test in Task 9.
4. A report whose GitHub link is a `/tree/<branch>/<subdir>` URL with trailing punctuation (`…/lab4).`) → link parsed without the punctuation, only files under the subdir sent. Test in Task 13.
5. Re-applying an old v7-style config (`review_mode: quiz_and_teacher_scores`) after v8 → still validates and grades identically (translation path). Test in Task 2 + Task 8.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/submissions_checker/services/requirements.py` (new) | Pure: `Requirement`, `Plan`, `parse`, `validate`, `from_config` (legacy translation), `Facts`, `evaluate`, `parse_scores_form`, `row_signature` |
| `src/submissions_checker/services/settle.py` (new) | DB: `gather_facts`, `settle`, `quiz_host`, `reject`, `reopen_after_new_version` |
| `src/submissions_checker/services/board_scores.py` (new) | Per-row teacher save (single + batch), extracted from `teacher_portal.teacher_save_scores` |
| `src/submissions_checker/services/llm_grading/github_code.py` (new) | Link extraction (DOCX/PDF), in-memory tarball fetch + filter |
| `src/submissions_checker/cli/backfill_requirements.py` (new) | Prod one-off: tick `accepted` on completed work, re-settle, assert grades unchanged |
| `core/state_machine.py` | Add settle events, then delete mode events (Task 12) |
| `workers/tasks/check_tasks.py`, `review_tasks.py` | Call `settle` instead of mode branches |
| `api/routes/student_quiz.py`, `services/quiz_open.py`, `services/quiz_regrade.py`, `services/quiz_grants.py` | Quiz gate by requirement state; outcome → `settle` |
| `api/routes/teacher_portal.py`, `templates/teacher_assignment.html`, `templates/teacher_submission_review.html` | Item columns, batch form, no approve |
| `api/routes/student_portal.py`, `templates/assignment_detail.html`, `api/schemas/student_portal.py` | Checklist, upload by `source` |
| `services/config_apply.py`, `services/llm_grading/config.py` | Validate/copy `requirements` + `source` |
| `services/gradebook.py`, `api/routes/teacher_classroom.py` | Read new breakdown |
| `services/llm_grading/runner.py`, `prompt.py`, `judge.py`, `templates/teacher_llm_report.html` | GitHub code in request + `draft.code_source` |
| `services/google/ingest.py` | New Classroom version → `settle.reopen_after_new_version` |
| `services/teacher_scores.py`, `services/grading.py` | Shrunk to legacy-translation helpers in Task 12 |

---

### Task 1: Requirement model, parsing and validation (new-style configs)

**Files:**
- Create: `src/submissions_checker/services/requirements.py`
- Test: `tests/unit/test_requirements_parse.py`

**Interfaces:**
- Produces:
  - `ItemType = Literal["quiz", "score", "check", "tests", "ai_review"]`
  - `@dataclass(frozen=True) class Requirement: key: str; type: ItemType; title: str; points: float; optional: bool = False; llm: str | None = None` (`points` is `max` for `score`)
  - `@dataclass(frozen=True) class Plan: items: tuple[Requirement, ...]; source: Literal["upload", "classroom"] | None; rounding: Literal["half_up", "bankers"] = "half_up"` with methods `has(type) -> bool`, `of(type) -> list[Requirement]`, `teacher_items() -> list[Requirement]` (score + check), `llm_items() -> list[Requirement]` (score with `llm`)
  - `class RequirementsError(ValueError)`
  - `parse(a_cfg: Mapping[str, Any]) -> Plan` — new-style only
  - `validate(code: str, a_cfg: Mapping[str, Any], *, has_quiz_block: bool) -> None`

- [ ] **Step 1: Write the failing tests**

```python
"""requirements.parse / validate: the new-style `requirements:` list."""

from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq

LAB4 = {
    "min_grade": 0,
    "max_grade": 16,
    "source": "classroom",
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5, "llm": "rubric text"},
        {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
        {"type": "check", "key": "accepted", "title": "Прийнято викладачем", "points": 0},
    ],
}


def test_parse_keeps_order_types_and_points() -> None:
    plan = rq.parse(LAB4)
    assert [(i.key, i.type, i.points, i.optional) for i in plan.items] == [
        ("quiz", "quiz", 8, False),
        ("report", "score", 5, False),
        ("star", "score", 3, True),
        ("accepted", "check", 0, False),
    ]
    assert plan.source == "classroom"
    assert plan.items[1].llm == "rubric text"
    assert [i.key for i in plan.teacher_items()] == ["report", "star", "accepted"]
    assert [i.key for i in plan.llm_items()] == ["report"]


def test_source_defaults_to_upload_when_code_items_present() -> None:
    cfg = {"max_grade": 10, "requirements": [{"type": "tests", "key": "t", "points": 10}]}
    assert rq.parse(cfg).source == "upload"


def test_source_defaults_to_none_without_code_items() -> None:
    cfg = {"max_grade": 2, "requirements": [{"type": "score", "key": "r", "title": "R", "max": 2}]}
    assert rq.parse(cfg).source is None


def test_validate_accepts_lab4() -> None:
    rq.validate("lab4", LAB4, has_quiz_block=True)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"max_grade": 17}, "must equal max_grade - min_grade"),
        ({"review_mode": "quiz_and_teacher_scores"}, "review_mode"),
        ({"source": "ftp"}, "source"),
    ],
)
def test_validate_rejects_bad_assignment(patch: dict, message: str) -> None:
    with pytest.raises(rq.RequirementsError, match=message):
        rq.validate("lab4", {**LAB4, **patch}, has_quiz_block=True)


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"type": "check", "key": "a", "title": "A", "max": 1}, "check 'a' takes points"),
        ({"type": "check", "key": "a", "title": "A", "points": 0, "llm": "x"}, "llm"),
        ({"type": "score", "key": "a", "max": 1}, "title"),
        ({"type": "score", "key": "A b", "title": "A", "max": 1}, "key"),
        ({"type": "magic", "key": "a", "points": 0}, "type"),
        ({"type": "score", "key": "report", "title": "dup", "max": 0}, "duplicate"),
    ],
)
def test_validate_rejects_bad_item(item: dict, message: str) -> None:
    cfg = {**LAB4, "requirements": [*LAB4["requirements"], item]}
    with pytest.raises(rq.RequirementsError, match=message):
        rq.validate("lab4", cfg, has_quiz_block=True)


def test_quiz_item_needs_quiz_block() -> None:
    with pytest.raises(rq.RequirementsError, match="quiz block"):
        rq.validate("lab4", LAB4, has_quiz_block=False)


def test_quiz_block_needs_quiz_item() -> None:
    cfg = {**LAB4, "max_grade": 8, "requirements": LAB4["requirements"][1:]}
    with pytest.raises(rq.RequirementsError, match="no quiz item"):
        rq.validate("lab4", cfg, has_quiz_block=True)


def test_code_items_refused_with_classroom_source() -> None:
    cfg = {**LAB4, "max_grade": 18, "requirements": [
        *LAB4["requirements"], {"type": "tests", "key": "t", "points": 2}]}
    with pytest.raises(rq.RequirementsError, match="not supported yet"):
        rq.validate("lab4", cfg, has_quiz_block=True)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen --extra dev pytest tests/unit/test_requirements_parse.py -q`
Expected: FAIL — `ModuleNotFoundError: submissions_checker.services.requirements`

- [ ] **Step 3: Implement**

```python
"""What must be done for an assignment's work to count, and what it is worth.

An assignment declares ``requirements:`` — independent items (quiz, teacher score, teacher
check, sandbox tests, AI review) done in any order. The grade appears once every required
item is done and is ``min_grade`` plus the items' points. Old ``review_mode`` configs are
translated into the same shape on load (``from_config``), so the rest of the app reads one
model. Pure and DB-free: config_apply, settle, the board and the student page share it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, TypeGuard

ItemType = Literal["quiz", "score", "check", "tests", "ai_review"]
ITEM_TYPES: frozenset[str] = frozenset({"quiz", "score", "check", "tests", "ai_review"})
CODE_TYPES: frozenset[str] = frozenset({"tests", "ai_review"})
TEACHER_TYPES: frozenset[str] = frozenset({"score", "check"})
SOURCES: frozenset[str] = frozenset({"upload", "classroom"})
_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_DEFAULT_TITLES = {"quiz": "Тест", "tests": "Тести", "ai_review": "AI-рев'ю"}


class RequirementsError(ValueError):
    """An assignment's requirements cannot be used as written."""


@dataclass(frozen=True)
class Requirement:
    key: str
    type: ItemType
    title: str
    points: float
    optional: bool = False
    llm: str | None = None


@dataclass(frozen=True)
class Plan:
    items: tuple[Requirement, ...]
    source: Literal["upload", "classroom"] | None
    rounding: Literal["half_up", "bankers"] = "half_up"

    def has(self, item_type: str) -> bool:
        return any(i.type == item_type for i in self.items)

    def of(self, item_type: str) -> list[Requirement]:
        return [i for i in self.items if i.type == item_type]

    def teacher_items(self) -> list[Requirement]:
        return [i for i in self.items if i.type in TEACHER_TYPES]

    def llm_items(self) -> list[Requirement]:
        return [i for i in self.items if i.type == "score" and i.llm]


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _item(where: str, raw: Mapping[str, Any]) -> Requirement:
    item_type = raw.get("type")
    if item_type not in ITEM_TYPES:
        raise RequirementsError(f"{where}: type {item_type!r} must be one of {sorted(ITEM_TYPES)}")
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise RequirementsError(f"{where}: key {key!r} must match [a-z0-9_]+")
    if item_type in TEACHER_TYPES and not str(raw.get("title") or "").strip():
        raise RequirementsError(f"{where}: {item_type} '{key}' needs a title")
    if item_type == "score":
        amount = raw.get("max")
        if not _is_int(amount) or amount < 0:
            raise RequirementsError(f"{where}: score '{key}' max must be a non-negative integer")
    else:
        if "max" in raw:
            raise RequirementsError(f"{where}: {item_type} '{key}' takes points, not max")
        amount = raw.get("points", 0)
        if not _is_int(amount) or amount < 0:
            raise RequirementsError(f"{where}: {item_type} '{key}' points must be >= 0")
    llm = raw.get("llm")
    if llm is not None and item_type != "score":
        raise RequirementsError(f"{where}: only score items take llm (got it on '{key}')")
    if llm is not None and not (isinstance(llm, str) and llm.strip()):
        raise RequirementsError(f"{where}: score '{key}' llm must be non-empty rubric text")
    return Requirement(
        key=key,
        type=item_type,  # type: ignore[arg-type]  # checked against ITEM_TYPES above
        title=str(raw.get("title") or _DEFAULT_TITLES.get(item_type, key)),
        points=amount,
        optional=bool(raw.get("optional", False)),
        llm=llm.strip() if isinstance(llm, str) else None,
    )


def parse(a_cfg: Mapping[str, Any]) -> Plan:
    """The plan of a new-style assignment (one with ``requirements:``)."""
    raw = a_cfg.get("requirements")
    if not isinstance(raw, list) or not raw:
        raise RequirementsError("requirements must be a non-empty list")
    items = tuple(_item("requirements", r or {}) for r in raw)
    source = a_cfg.get("source")
    if source is None and any(i.type in CODE_TYPES for i in items):
        source = "upload"
    if source is not None and source not in SOURCES:
        raise RequirementsError(f"source {source!r} must be one of {sorted(SOURCES)}")
    return Plan(items=items, source=source)


def validate(code: str, a_cfg: Mapping[str, Any], *, has_quiz_block: bool) -> None:
    """Raise RequirementsError unless a new-style assignment can be graded as written."""
    where = f"assignment '{code}'"
    if "review_mode" in a_cfg:
        raise RequirementsError(f"{where}: drop review_mode; requirements replace it")
    try:
        plan = parse(a_cfg)
    except RequirementsError as exc:
        raise RequirementsError(f"{where}: {exc}") from exc
    seen: set[str] = set()
    for item in plan.items:
        if item.key in seen:
            raise RequirementsError(f"{where}: duplicate requirement key '{item.key}'")
        seen.add(item.key)
    if plan.has("quiz") and not has_quiz_block:
        raise RequirementsError(f"{where}: a quiz item needs a quiz block with questions")
    if has_quiz_block and not plan.has("quiz"):
        raise RequirementsError(f"{where}: has a quiz block but no quiz item")
    if len(plan.of("quiz")) > 1:
        raise RequirementsError(f"{where}: at most one quiz item")
    if plan.source == "classroom" and any(i.type in CODE_TYPES for i in plan.items):
        raise RequirementsError(
            f"{where}: tests/ai_review on Classroom works are not supported yet"
        )
    total = sum(i.points for i in plan.items)
    span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    if total != span:
        raise RequirementsError(
            f"{where}: requirement points ({total:g}) must equal max_grade - min_grade ({span})"
        )
```

(`math`, `Decimal`, `ROUND_HALF_UP` are used by Task 3; leave the imports — ruff will flag them now, so add them in Task 3 instead if ruff runs between tasks.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --frozen --extra dev pytest tests/unit/test_requirements_parse.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/submissions_checker/services/requirements.py tests/unit/test_requirements_parse.py
git commit -m "Add the requirements list model and its config validation

Review modes hard-code a pipeline; a list of independent items lets an
assignment declare exactly what has to be done for its work to count.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Translate legacy `review_mode` configs

**Files:**
- Modify: `src/submissions_checker/services/requirements.py`
- Test: `tests/unit/test_requirements_translate.py`

**Interfaces:**
- Consumes: `Plan`, `Requirement`, `parse` (Task 1); `teacher_scores.criteria`, `teacher_scores.MODE` (existing).
- Produces: `from_config(a_cfg: Mapping[str, Any] | None) -> Plan` — the only reader of `review_mode` outside `teacher_scores`/`config_apply`. Translated plans set `rounding="bankers"` for weighted modes, `"half_up"` for `quiz_and_teacher_scores`.

Mode → items (weights: tests = `code_weight*works_weight`, ai = `code_weight*quality_weight`, quiz = `quiz_weight`; defaults code 1, quiz 0, works 1, quality 0; `points = span*w/Σw`, all 0 when `Σw == 0`):

| mode | items | source |
|---|---|---|
| `tests_only` | tests | upload |
| `tests_then_ai` | tests, ai_review | upload |
| `tests_then_teacher` | tests, check accepted(0) | upload |
| `tests_then_ai_then_teacher`, `tests_then_ai_teacher` | tests, ai_review, check | upload |
| `tests_then_quiz` | tests, quiz | upload |
| `tests_then_ai_then_quiz` | tests, ai_review, quiz | upload |
| `quiz_only` | quiz | upload |
| `quiz_then_teacher` | quiz, check | upload |
| `quiz_and_teacher_scores` | quiz(quiz_points), score per `teacher_criteria` (llm = criterion `requirements` unless `llm: false`) | classroom if `llm_grading.enabled` else None |
| missing/unknown | as `tests_only` | upload |

- [ ] **Step 1: Write the failing tests** — the oracle is the old grade code, still in the repo.

```python
"""from_config: legacy review_mode configs become plans that grade exactly as before."""

from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq
from submissions_checker.services import teacher_scores
from submissions_checker.services.grading import compute_grade

SCORED = {
    "review_mode": "quiz_and_teacher_scores",
    "llm_grading": {"enabled": True, "source": "google_classroom", "task": "t"},
    "grading": {
        "quiz_points": 8,
        "teacher_criteria": [
            {"key": "report", "title": "Звіт", "max": 5, "requirements": "rubric"},
            {"key": "star", "title": "Зірочка", "max": 3, "optional": True, "llm": False},
        ],
    },
}


def test_new_style_config_is_parsed_not_translated() -> None:
    cfg = {"max_grade": 1, "requirements": [{"type": "check", "key": "a", "title": "A", "points": 1}]}
    assert rq.from_config(cfg) == rq.parse(cfg)


def test_scored_mode_translation() -> None:
    plan = rq.from_config(SCORED)
    assert [(i.key, i.type, i.points, i.optional, i.llm) for i in plan.items] == [
        ("quiz", "quiz", 8, False, None),
        ("report", "score", 5, False, "rubric"),
        ("star", "score", 3, True, None),
    ]
    assert plan.source == "classroom"
    assert plan.rounding == "half_up"


@pytest.mark.parametrize(
    ("mode", "types"),
    [
        ("tests_only", ["tests"]),
        ("tests_then_ai", ["tests", "ai_review"]),
        ("tests_then_teacher", ["tests", "check"]),
        ("tests_then_ai_then_teacher", ["tests", "ai_review", "check"]),
        ("tests_then_ai_teacher", ["tests", "ai_review", "check"]),
        ("tests_then_quiz", ["tests", "quiz"]),
        ("tests_then_ai_then_quiz", ["tests", "ai_review", "quiz"]),
        ("quiz_only", ["quiz"]),
        ("quiz_then_teacher", ["quiz", "check"]),
        (None, ["tests"]),
    ],
)
def test_weighted_modes_translate_to_items(mode: str | None, types: list[str]) -> None:
    cfg = {} if mode is None else {"review_mode": mode}
    plan = rq.from_config(cfg)
    assert [i.type for i in plan.items] == types
    assert plan.source == "upload"
    assert plan.rounding == "bankers"


def _oracle_cases() -> list[tuple[str, dict, float | None, float | None, float | None]]:
    g = {"code_weight": 0.6, "quiz_weight": 0.4, "code": {"works_weight": 0.7, "quality_weight": 0.3}}
    return [
        ("tests_only", {}, 73.0, None, None),
        ("tests_then_ai", g, 55.0, 81.0, None),
        ("tests_then_quiz", g, 100.0, None, 62.5),
        ("tests_then_ai_then_quiz", g, 40.0, 90.0, 75.0),
        ("quiz_only", {"quiz_weight": 1.0}, None, None, 87.5),
    ]


@pytest.mark.parametrize(("mode", "grading", "works", "ai", "quiz"), _oracle_cases())
@pytest.mark.parametrize(("lo", "hi"), [(0, 100), (1, 5), (60, 100)])
def test_translated_weighted_grade_equals_old_compute_grade(
    mode: str, grading: dict, works: float | None, ai: float | None, quiz: float | None,
    lo: int, hi: int,
) -> None:
    cfg = {"review_mode": mode, "grading": grading}
    old = compute_grade(grading, lo, hi, works_pct=works, ai_mark=ai, quiz_pct=quiz).grade
    facts = rq.Facts(
        quiz=("done", quiz) if quiz is not None else ("pending", None),
        tests=("done", works) if works is not None else ("pending", None),
        ai_review=("done", ai) if ai is not None else ("pending", None),
        checks_done=frozenset({"accepted"}),
    )
    assert rq.evaluate(rq.from_config(cfg), facts, lo, hi, round_up=False).grade == old


@pytest.mark.parametrize("quiz_pct", [0.0, 31.25, 56.25, 68.75, 100.0])
@pytest.mark.parametrize("round_up", [False, True])
def test_translated_scored_grade_equals_old_compute(quiz_pct: float, round_up: bool) -> None:
    scores = {"report": 4, "star": 2}
    old = teacher_scores.compute(
        SCORED["grading"], 0, 16, quiz_pct=quiz_pct, scores=scores, round_up=round_up
    )["grade"]
    facts = rq.Facts(quiz=("done", quiz_pct), scores=scores)
    assert rq.evaluate(rq.from_config(SCORED), facts, 0, 16, round_up=round_up).grade == old
```

(These tests use `Facts`/`evaluate` from Task 3; run Task 2's translation tests after Task 3 lands, or write Task 3 first. Recommended order: implement Task 2 Step 3 now, run only the first three tests, then finish Task 3 and run the whole file.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen --extra dev pytest tests/unit/test_requirements_translate.py -q -k "translat or new_style"`
Expected: FAIL — `AttributeError: module ... has no attribute 'from_config'`

- [ ] **Step 3: Implement** (append to `requirements.py`)

```python
_ACCEPTED = Requirement(key="accepted", type="check", title="Прийнято викладачем", points=0)
_LEGACY_ITEMS: dict[str, tuple[str, ...]] = {
    "tests_only": ("tests",),
    "tests_then_ai": ("tests", "ai_review"),
    "tests_then_teacher": ("tests", "check"),
    "tests_then_ai_then_teacher": ("tests", "ai_review", "check"),
    "tests_then_ai_teacher": ("tests", "ai_review", "check"),
    "tests_then_quiz": ("tests", "quiz"),
    "tests_then_ai_then_quiz": ("tests", "ai_review", "quiz"),
    "quiz_only": ("quiz",),
    "quiz_then_teacher": ("quiz", "check"),
}


def _translate_scored(a_cfg: Mapping[str, Any]) -> Plan:
    from submissions_checker.services import teacher_scores  # legacy reader, no cycle

    grading = a_cfg.get("grading") or {}
    raw_by_key = {r.get("key"): r for r in grading.get("teacher_criteria") or []}
    items = [Requirement("quiz", "quiz", "Тест", int(grading.get("quiz_points", 0)))]
    for c in teacher_scores.criteria(grading):
        raw = raw_by_key.get(c.key) or {}
        llm = None if raw.get("llm") is False else (str(raw.get("requirements") or "").strip() or None)
        items.append(Requirement(c.key, "score", c.title, c.max, c.optional, llm))
    llm_on = (a_cfg.get("llm_grading") or {}).get("enabled") is True
    return Plan(items=tuple(items), source="classroom" if llm_on else None)


def _translate_weighted(a_cfg: Mapping[str, Any], mode: str) -> Plan:
    grading = a_cfg.get("grading") or {}
    code = grading.get("code") or {}
    code_w = float(grading.get("code_weight", 1.0))
    weights = {
        "tests": code_w * float(code.get("works_weight", 1.0)),
        "ai_review": code_w * float(code.get("quality_weight", 0.0)),
        "quiz": float(grading.get("quiz_weight", 0.0)),
    }
    kinds = _LEGACY_ITEMS.get(mode, _LEGACY_ITEMS["tests_only"])
    total_w = sum(max(weights[k], 0.0) for k in kinds if k != "check")
    span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    items: list[Requirement] = []
    for k in kinds:
        if k == "check":
            items.append(_ACCEPTED)
            continue
        share = max(weights[k], 0.0) / total_w if total_w > 0 else 0.0
        items.append(Requirement(k, k, _DEFAULT_TITLES[k], span * share))  # type: ignore[arg-type]
    return Plan(items=tuple(items), source="upload", rounding="bankers")


def from_config(a_cfg: Mapping[str, Any] | None) -> Plan:
    """The assignment's plan: its ``requirements`` list, or its old review mode translated."""
    cfg = a_cfg or {}
    if "requirements" in cfg:
        return parse(cfg)
    mode = str(cfg.get("review_mode") or "tests_only")
    if mode == "quiz_and_teacher_scores":
        return _translate_scored(cfg)
    return _translate_weighted(cfg, mode)
```

Note: the assignment's `min_grade`/`max_grade` live on `SubjectsAssignment` columns, not in `config`. `from_config` callers that translate weighted modes must pass the span: add keyword `span: int | None = None` to `from_config` and `_translate_weighted`, used instead of `max_grade - min_grade` from the dict when given. All DB callers (`settle`, board, student page) pass `span=asg.max_grade - asg.min_grade`. Update the oracle test to call `rq.from_config(cfg, span=hi - lo)`.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/unit/test_requirements_translate.py -q -k "translat or new_style"` → PASS. (The oracle tests pass after Task 3.)

- [ ] **Step 5: Commit** — `Translate old review modes into requirement plans` (body: old configs keep grading the same, so the rest of the app reads one model).

---

### Task 3: Evaluate facts into item states and a grade

**Files:**
- Modify: `src/submissions_checker/services/requirements.py`
- Test: `tests/unit/test_requirements_evaluate.py` (+ the oracle tests of Task 2 now run)

**Interfaces:**
- Produces:
  - `State = Literal["done", "pending", "running", "failed"]`
  - `@dataclass(frozen=True) class Facts: quiz: tuple[State, float | None] = ("pending", None); tests: tuple[State, float | None] = ("pending", None); ai_review: tuple[State, float | None] = ("pending", None); scores: Mapping[str, int] = field(default_factory=dict); checks_done: frozenset[str] = frozenset()` — `scores` holds score items; a check counts as done when `scores.get(key) == 1` **or** key in `checks_done` (the latter only for translated `accepted` in the oracle test).
  - `@dataclass(frozen=True) class ItemResult: key: str; type: str; title: str; state: State; points: float; max: float; optional: bool`
  - `@dataclass(frozen=True) class Evaluation: items: tuple[ItemResult, ...]; complete: bool; failed: bool; grade: int | None; breakdown: dict[str, Any] | None` and `pending_teacher() -> bool` (a required score/check not done), `first_pending() -> ItemResult | None`
  - `evaluate(plan: Plan, facts: Facts, min_grade: int, max_grade: int, *, round_up: bool) -> Evaluation`
  - Breakdown shape: `{"grade", "mode": "requirements", "quiz_score", "works_score", "quality_score", "items": [{"key","type","title","state","points","max"}]}` (`points` rounded to 2 decimals).

- [ ] **Step 1: Write the failing tests**

```python
"""requirements.evaluate: item states, completion and the grade."""

from __future__ import annotations

from submissions_checker.services import requirements as rq

PLAN = rq.parse({
    "max_grade": 16,
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5},
        {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
        {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
    ],
})


def ev(facts: rq.Facts, *, round_up: bool = False) -> rq.Evaluation:
    return rq.evaluate(PLAN, facts, 0, 16, round_up=round_up)


def test_nothing_done_is_pending_without_grade() -> None:
    e = ev(rq.Facts())
    assert [i.state for i in e.items] == ["pending"] * 4
    assert (e.complete, e.failed, e.grade, e.breakdown) == (False, False, None, None)
    assert e.pending_teacher() is True


def test_quiz_and_report_without_check_is_not_complete() -> None:
    e = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4}))
    assert e.complete is False and e.grade is None
    assert e.first_pending().key == "accepted"


def test_all_required_done_gives_grade_and_optional_adds() -> None:
    e = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4, "accepted": 1}))
    assert e.complete and e.grade == 10  # 6 + 4 + 0
    e2 = ev(rq.Facts(quiz=("done", 75.0), scores={"report": 4, "star": 2, "accepted": 1}))
    assert e2.grade == 12
    assert [i["key"] for i in e2.breakdown["items"]] == ["quiz", "report", "star", "accepted"]
    assert e2.breakdown["mode"] == "requirements" and e2.breakdown["quiz_score"] == 75.0


def test_half_up_and_squad_ceil() -> None:
    facts = rq.Facts(quiz=("done", 56.25), scores={"report": 0, "accepted": 1})  # 4.5 pts
    assert ev(facts).grade == 5
    facts = rq.Facts(quiz=("done", 53.0), scores={"report": 0, "accepted": 1})  # 4.24 pts
    assert ev(facts).grade == 4
    assert ev(facts, round_up=True).grade == 5


def test_failed_required_item_fails_the_work() -> None:
    e = ev(rq.Facts(quiz=("failed", None), scores={"report": 5, "accepted": 1}))
    assert e.failed and not e.complete and e.grade is None


def test_failed_optional_item_does_not_fail() -> None:
    plan = rq.parse({"max_grade": 3, "requirements": [
        {"type": "tests", "key": "t", "points": 2, "optional": True},
        {"type": "check", "key": "a", "title": "A", "points": 1},
    ]})
    e = rq.evaluate(plan, rq.Facts(tests=("failed", None), scores={"a": 1}), 0, 3, round_up=False)
    assert e.complete and not e.failed and e.grade == 1


def test_tests_and_ai_points_scale_by_percent() -> None:
    plan = rq.parse({"max_grade": 12, "requirements": [
        {"type": "tests", "key": "t", "points": 10},
        {"type": "ai_review", "key": "ai", "points": 2},
    ]})
    e = rq.evaluate(plan, rq.Facts(tests=("done", 80.0), ai_review=("done", 50.0)), 0, 12, round_up=False)
    assert e.grade == 9 and e.breakdown["works_score"] == 80.0 and e.breakdown["quality_score"] == 50.0


def test_running_item_blocks_completion() -> None:
    plan = rq.parse({"max_grade": 10, "requirements": [{"type": "tests", "key": "t", "points": 10}]})
    e = rq.evaluate(plan, rq.Facts(tests=("running", None)), 0, 10, round_up=False)
    assert not e.complete and not e.failed and e.items[0].state == "running"


def test_grade_is_clamped_to_range() -> None:
    plan = rq.parse({"min_grade": 1, "max_grade": 2, "requirements": [
        {"type": "score", "key": "s", "title": "S", "max": 1}]})
    assert rq.evaluate(plan, rq.Facts(scores={"s": 1}), 1, 2, round_up=True).grade == 2
```

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/unit/test_requirements_evaluate.py -q` → FAIL (`no attribute 'Facts'`).

- [ ] **Step 3: Implement** (append to `requirements.py`; add `from dataclasses import field`)

```python
State = Literal["done", "pending", "running", "failed"]


@dataclass(frozen=True)
class Facts:
    quiz: tuple[State, float | None] = ("pending", None)
    tests: tuple[State, float | None] = ("pending", None)
    ai_review: tuple[State, float | None] = ("pending", None)
    scores: Mapping[str, int] = field(default_factory=dict)
    checks_done: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ItemResult:
    key: str
    type: str
    title: str
    state: State
    points: float
    max: float
    optional: bool


@dataclass(frozen=True)
class Evaluation:
    items: tuple[ItemResult, ...]
    complete: bool
    failed: bool
    grade: int | None
    breakdown: dict[str, Any] | None

    def pending_teacher(self) -> bool:
        return any(
            i.type in TEACHER_TYPES and not i.optional and i.state != "done" for i in self.items
        )

    def first_pending(self) -> ItemResult | None:
        return next((i for i in self.items if not i.optional and i.state != "done"), None)


def _result(item: Requirement, facts: Facts) -> ItemResult:
    state: State
    earned = 0.0
    if item.type in ("quiz", "tests", "ai_review"):
        state, pct = getattr(facts, item.type)
        if state == "done":
            earned = item.points * float(pct or 0.0) / 100.0
    elif item.type == "score":
        value = facts.scores.get(item.key)
        state = "done" if value is not None else "pending"
        earned = float(value or 0)
    else:  # check
        ticked = facts.scores.get(item.key) == 1 or item.key in facts.checks_done
        state = "done" if ticked else "pending"
        earned = float(item.points) if ticked else 0.0
    return ItemResult(item.key, item.type, item.title, state, earned, item.points, item.optional)


def _round(total: float, rounding: str, round_up: bool) -> int:
    total = round(total, 6)
    if round_up:
        return math.ceil(total)
    if rounding == "bankers":
        return round(total)
    return int(Decimal(str(total)).quantize(Decimal(1), ROUND_HALF_UP))


def evaluate(
    plan: Plan, facts: Facts, min_grade: int, max_grade: int, *, round_up: bool
) -> Evaluation:
    """Each item's state and points; a grade only once every required item is done."""
    items = tuple(_result(i, facts) for i in plan.items)
    required = [i for i in items if not i.optional]
    failed = any(i.state == "failed" for i in required)
    complete = not failed and all(i.state == "done" for i in required)
    if not complete:
        return Evaluation(items, False, failed, None, None)
    earned = sum(i.points for i in items if i.state == "done")
    grade = max(min_grade, min(max_grade, _round(min_grade + earned, plan.rounding, round_up)))
    breakdown = {
        "grade": grade,
        "mode": "requirements",
        "quiz_score": facts.quiz[1] if facts.quiz[0] == "done" else None,
        "works_score": facts.tests[1] if facts.tests[0] == "done" else None,
        "quality_score": facts.ai_review[1] if facts.ai_review[0] == "done" else None,
        "items": [
            {"key": i.key, "type": i.type, "title": i.title, "state": i.state,
             "points": round(i.points, 2), "max": i.max}
            for i in items
        ],
    }
    return Evaluation(items, True, False, grade, breakdown)
```

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/unit/test_requirements_evaluate.py tests/unit/test_requirements_translate.py tests/unit/test_requirements_parse.py -q` → PASS. If an oracle case differs, the cause is rounding: print both totals; the old weighted path rounds `min + span*blend/100` with `round()`; make sure `_round` uses `bankers` for translated plans.

- [ ] **Step 5: Commit** — `Evaluate requirement facts into a grade` (body: one formula for every assignment; matches the old blends on a table of cases).

---

### Task 4: Teacher form parsing and the row signature

**Files:**
- Modify: `src/submissions_checker/services/requirements.py`
- Test: `tests/unit/test_requirements_form.py`

**Interfaces:**
- Produces:
  - `class ScoreError(ValueError)` (message shown to the teacher)
  - `parse_scores_form(plan: Plan, form: Mapping[str, str], *, prefix: str = "score_", locked: Mapping[str, int] | None = None) -> dict[str, int]` — score items: empty → left out, else whole number ≤ max; check items: `"1"`/`"on"` → 1, missing → left out; any check already `1` in `locked` is kept at 1 regardless of the form.
  - `row_signature(values: Mapping[str, str]) -> str` — `sha256("\n".join(f"{k}={v}" for k, v in sorted(values.items())))[:16]`.

- [ ] **Step 1: Failing tests**

```python
from __future__ import annotations

import pytest

from submissions_checker.services import requirements as rq

PLAN = rq.parse({"max_grade": 8, "requirements": [
    {"type": "score", "key": "report", "title": "Звіт", "max": 5},
    {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
    {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
]})


def test_scores_and_check_parsed() -> None:
    form = {"score_report": " 4 ", "score_star": "", "score_accepted": "on"}
    assert rq.parse_scores_form(PLAN, form) == {"report": 4, "accepted": 1}


def test_custom_prefix() -> None:
    assert rq.parse_scores_form(PLAN, {"s7__report": "3"}, prefix="s7__") == {"report": 3}


@pytest.mark.parametrize("raw", ["6", "-1", "4.5", "²", "abc"])
def test_bad_score_rejected(raw: str) -> None:
    with pytest.raises(rq.ScoreError, match="Звіт"):
        rq.parse_scores_form(PLAN, {"score_report": raw})


def test_saved_check_cannot_be_cleared() -> None:
    assert rq.parse_scores_form(PLAN, {"score_report": "2"}, locked={"accepted": 1}) == {
        "report": 2, "accepted": 1}


def test_row_signature_is_order_independent() -> None:
    a = rq.row_signature({"report": "4", "accepted": ""})
    assert a == rq.row_signature({"accepted": "", "report": "4"})
    assert a != rq.row_signature({"report": "5", "accepted": ""})
    assert len(a) == 16
```

- [ ] **Step 2: Run** → FAIL (`no attribute 'parse_scores_form'`).

- [ ] **Step 3: Implement**

```python
class ScoreError(ValueError):
    """Teacher input that cannot be stored as points."""


def parse_scores_form(
    plan: Plan,
    form: Mapping[str, str],
    *,
    prefix: str = "score_",
    locked: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Teacher items from a board form. A saved check stays saved: it cannot be unticked."""
    out: dict[str, int] = {}
    for item in plan.teacher_items():
        raw = str(form.get(f"{prefix}{item.key}") or "").strip()
        if item.type == "check":
            if raw in ("1", "on") or (locked or {}).get(item.key) == 1:
                out[item.key] = 1
            continue
        if raw == "":
            continue
        # isascii+isdecimal, not isdigit: "²".isdigit() is True and int() would raise.
        if not (raw.isascii() and raw.isdecimal()):
            raise ScoreError(f"{item.title}: '{raw}' is not a whole number")
        value = int(raw)
        if value > item.points:
            raise ScoreError(f"{item.title}: {value} is above the maximum {item.points:g}")
        out[item.key] = value
    return out


def row_signature(values: Mapping[str, str]) -> str:
    """Fingerprint of a board row as rendered, so a batch save can skip untouched rows."""
    import hashlib

    blob = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `Parse board scores with a check that cannot be unticked`.

---

### Task 5: `settle` — facts from the DB, grade and status

**Files:**
- Modify: `src/submissions_checker/core/state_machine.py` (add events; keep old ones until Task 12)
- Create: `src/submissions_checker/services/settle.py`
- Test: `tests/unit/test_state_machine.py` (add cases), `tests/functional/test_settle.py`

**Interfaces:**
- Consumes: `requirements.from_config/evaluate/Facts` (Tasks 2–3); `squads.scope_sa_ids`, `squads.active_squad`, `squads.member_sa_ids`, `squads.quiz_complete`, `grading.squad_quiz_pct`, `quiz_grants.base_max_attempts/effective_max_attempts/is_exhausted` (existing).
- Produces:
  - state machine events, each allowed from `{PENDING, QUIZ_SENT, AWAITING_TEACHER_REVIEW, COMPLETED, FAILED}` (settled states; `PENDING` only for upload-less `QUIZ_ONLY` — guarded in `settle`): `requirements_met → COMPLETED`, `requirement_failed → FAILED`, `awaiting_teacher → AWAITING_TEACHER_REVIEW`, `awaiting_quiz → QUIZ_SENT`; plus `processing_done` from `TESTING` and `AI_REVIEWING` → `AWAITING_TEACHER_REVIEW` (always followed by `settle` in the same transaction).
  - `SETTLED = frozenset({...})`, `PROCESSING = frozenset({VALIDATING, TESTING, AWAITING_AI_REVIEW, AI_REVIEWING})`, `CHECK_FAILED = frozenset({VALIDATION_FAILED, TEST_FAILED, AI_REVIEW_FAILED})` in `settle.py`.
  - `@dataclass(frozen=True) class SettleResult: submission_id: int | None; old_status: SubmissionStatus | None; new_status: SubmissionStatus | None; evaluation: requirements.Evaluation` with property `completed_now: bool` (`new == COMPLETED != old`).
  - `def plan_for(asg: SubjectsAssignment) -> requirements.Plan` (`from_config(asg.config, span=asg.max_grade - asg.min_grade)`)
  - `async def gather_facts(db, sa: StudentAssignment, asg: SubjectsAssignment) -> tuple[requirements.Facts, list[Submission], Squad | None, list[int]]` (facts, scope submissions newest first, squad, scope SA ids)
  - `async def quiz_host(db, subs: list[Submission]) -> Submission | None` — latest submission that has any quiz attempt, else the latest submission.
  - `async def settle(db, sa: StudentAssignment) -> SettleResult` — does not commit.
  - `REJECTED_KEY = "rejected"` in `source_metadata`.

Facts rules (implement exactly):
- scope = `squads.scope_sa_ids(db, sa.student_id, asg.subject_id, asg.id)`; subs = all submissions with `students_assignment_id in scope`, ordered `created_at desc, id desc`; latest = `subs[0]`.
- quiz: `done` if any sub has `squads.quiz_complete(db, sub)`; pct = solo: best `score/max_score*100` over passed attempts of all subs; squad: `grading.squad_quiz_pct(attempts of the completing sub)[0]`. `failed` if not done and host exists and `base_max_attempts(host)` is not None and some student on the host (`quiz_grants._students_on`) is exhausted (`is_exhausted(attempts_of(host, sid), effective_max_attempts(base, host, sid))`) without having passed. Else `pending`.
- tests/ai_review: from the latest `ZIP_UPLOAD` sub (`code`): none → pending; status `PENDING/VALIDATING/TESTING` → tests running; `VALIDATION_FAILED/TEST_FAILED` → tests failed; `test_results.score/max_score` present → tests done with pct. ai: `AWAITING_AI_REVIEW/AI_REVIEWING` or tests not done → running/pending accordingly; `AI_REVIEW_FAILED` → failed; `ai_review.code_mark` number → done.
- scores: `sa.teacher_scores or {}` (written to every member by the board).

Status rules in `settle`:
1. No submission: if evaluation complete → create an upload-less `Submission(source_type=QUIZ_ONLY, status=PENDING, plugin_config_id=<latest config id>, squad_id=squad.id if squad else None, test_results={"skipped": True, "reason": "requirements"})`, flush, continue with it; else write nothing, return.
2. Lock: `await db.refresh(latest, attribute_names=["status", "source_metadata"], with_for_update=True)`.
3. If `latest.status in PROCESSING or CHECK_FAILED` or (`PENDING` and `source_type == ZIP_UPLOAD`): no status change; return.
4. If `latest.status == COMPLETED`: when complete, rewrite grade + breakdown (fan-out below); when not complete, log `settle_completed_incomplete` warning and change nothing. Never leave COMPLETED.
5. Target: rejected (`(latest.source_metadata or {}).get(REJECTED_KEY)`) → `requirement_failed`; complete → `requirements_met`; failed → `requirement_failed`; `pending_teacher()` or no quiz item → `awaiting_teacher`; else `awaiting_quiz`. Apply via `transition()` only when the target status differs.
6. On `requirements_met`: write `grade` to every scope row (`StudentAssignment.grade`), set `latest.grade_breakdown = breakdown` (+ `"squad": {"unified": True, "squad_id", "members"}` like `finalize_grade` for squads).
7. On entering `AWAITING_TEACHER_REVIEW` from another status: `await enqueue_teacher_review_notification(db, latest.id)` (import from where `check_tasks` imports it).

- [ ] **Step 1: State-machine tests** (append to `tests/unit/test_state_machine.py`)

```python
import pytest

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models.enums import SubmissionStatus as S

SETTLED = [S.PENDING, S.QUIZ_SENT, S.AWAITING_TEACHER_REVIEW, S.COMPLETED, S.FAILED]


@pytest.mark.parametrize("start", SETTLED)
@pytest.mark.parametrize(
    ("event", "end"),
    [("requirements_met", S.COMPLETED), ("requirement_failed", S.FAILED),
     ("awaiting_teacher", S.AWAITING_TEACHER_REVIEW), ("awaiting_quiz", S.QUIZ_SENT)],
)
def test_settle_events_from_every_settled_state(start, event, end) -> None:
    sub = type("S", (), {"status": start, "id": 1})()
    transition(sub, event)
    assert sub.status == end


@pytest.mark.parametrize("start", [S.TESTING, S.AI_REVIEWING])
def test_processing_done(start) -> None:
    sub = type("S", (), {"status": start, "id": 1})()
    transition(sub, "processing_done")
    assert sub.status == S.AWAITING_TEACHER_REVIEW
```

(Check how existing tests in that file build a submission stub and reuse that helper instead of `type(...)` if one exists.)

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/unit/test_state_machine.py -q` → FAIL (`InvalidTransitionError`).

- [ ] **Step 3: Implement the events** — in `core/state_machine.py`, after `_TRANSITIONS` is defined and before the `requeue_checks` loop:

```python
# Requirements model: `settle` decides the status from what is done, so its four events are
# allowed from every settled state (and are idempotent self-loops where the status is
# unchanged). `processing_done` ends the upload pipeline; settle runs right after it.
for _status in (
    SubmissionStatus.PENDING,
    SubmissionStatus.QUIZ_SENT,
    SubmissionStatus.AWAITING_TEACHER_REVIEW,
    SubmissionStatus.COMPLETED,
    SubmissionStatus.FAILED,
):
    _TRANSITIONS.setdefault(_status, {}).update(
        {
            "requirements_met": SubmissionStatus.COMPLETED,
            "requirement_failed": SubmissionStatus.FAILED,
            "awaiting_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
            "awaiting_quiz": SubmissionStatus.QUIZ_SENT,
        }
    )
for _status in (SubmissionStatus.TESTING, SubmissionStatus.AI_REVIEWING):
    _TRANSITIONS[_status]["processing_done"] = SubmissionStatus.AWAITING_TEACHER_REVIEW
```

Run Step 2 again → PASS.

- [ ] **Step 4: Functional tests for settle** — `tests/functional/test_settle.py`. Reuse the arrange helpers style of `tests/functional/test_quiz_and_teacher_scores.py` (copy `_arrange_scored_no_sub`/`_arrange_scored`/`_attempt` into a shared `tests/functional/_scored_helpers.py` and import from both files; the assignment config becomes the new-style dict below).

```python
"""settle(): the one place a student's assignment gets its grade and status."""

from __future__ import annotations

import asyncio

import pytest

from submissions_checker.db.models import StudentAssignment
from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services import settle
from tests.functional._scored_helpers import arrange, attempt, squad_of_two

pytestmark = pytest.mark.asyncio

CFG = {
    "source": "classroom",
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5},
        {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
    ],
}


async def test_quiz_passed_waits_for_teacher(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert res.evaluation.first_pending().key == "report"
    await db.refresh(sa)
    assert sa.grade is None


async def test_scores_then_check_complete_in_any_order(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                           scores={"report": 4, "accepted": 1})
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.QUIZ_SENT  # only the quiz left
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.completed_now
    await db.refresh(sa)
    assert sa.grade == 10  # 6 + 4 + 0
    assert sub.grade_breakdown["mode"] == "requirements"


async def test_exhausted_quiz_fails(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG, max_attempts=1)
    await attempt(db, sub, student.id, score=0, max_score=4, passed=False)
    assert (await settle.settle(db, sa)).new_status == SubmissionStatus.FAILED


async def test_rejected_stays_failed(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                           scores={"report": 4, "accepted": 1})
    sub.source_metadata = {settle.REJECTED_KEY: {"reason": "copy"}}
    await attempt(db, sub, student.id, score=4, max_score=4, passed=True)
    assert (await settle.settle(db, sa)).new_status == SubmissionStatus.FAILED


async def test_completed_never_leaves_completed(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                           status=SubmissionStatus.COMPLETED, scores={"report": 4})
    res = await settle.settle(db, sa)  # check missing: incomplete, but already graded
    assert res.new_status == SubmissionStatus.COMPLETED


async def test_teacher_only_plan_without_submission_creates_one(db, teacher, make_student) -> None:
    cfg = {"requirements": [{"type": "score", "key": "r", "title": "R", "max": 16}]}
    _, _, _, sa = await arrange(db, teacher, make_student, config=cfg, with_submission=False,
                                scores={"r": 11})
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 11


async def test_squad_concurrent_settle_one_transition(functional_sessionmaker, db, teacher,
                                                      make_student) -> None:
    sa_a, sa_b, sub = await squad_of_two(db, teacher, make_student, config=CFG,
                                         scores={"report": 5, "accepted": 1}, both_passed=True)

    async def run(sa_id: int) -> SubmissionStatus | None:
        async with functional_sessionmaker() as s:
            row = await s.get(StudentAssignment, sa_id)
            res = await settle.settle(s, row)
            await s.commit()
            return res.new_status

    statuses = await asyncio.gather(run(sa_a.id), run(sa_b.id))
    assert statuses == [SubmissionStatus.COMPLETED, SubmissionStatus.COMPLETED]
    for sa_id in (sa_a.id, sa_b.id):
        async with functional_sessionmaker() as s:
            assert (await s.get(StudentAssignment, sa_id)).grade == 13  # ceil(8) + 5 + 0
```

Write `tests/functional/_scored_helpers.py` with:
- `arrange(db, teacher, make_student, *, config, scores=None, status=QUIZ_SENT, max_attempts=2, with_submission=True)` → `(subject, asg, student, sa, sub)` or `(subject, asg, student, sa)`; subject with plugin config `{"assignments": {"l1": {**config, "quiz": QUIZ_with(max_attempts)}}}`, assignment `min_grade=0, max_grade=16, code="l1", config=config`; QUIZ_ONLY submission in `status`.
- `attempt(db, sub, student_id, *, score, max_score, passed)` → COMPLETED `QuizAttempt` (copy the body of `_attempt` from `test_quiz_and_teacher_scores.py`).
- `squad_of_two(...)` → subject with `squad_max_size=2`, a `Squad` + two `SquadMember`s (see `tests/functional/test_squad_quiz.py` for the arrange), shared submission with `squad_id`, each member's SA row carrying `scores`, a passed attempt per member (`student_id` set).

- [ ] **Step 5: Run** `uv run --frozen --extra dev pytest tests/functional/test_settle.py -q` → FAIL (`cannot import name 'settle'`).

- [ ] **Step 6: Implement `services/settle.py`**

```python
"""settle(): set a student's grade and submission status from what is done.

Called after every event that can change a requirement — a quiz finishing or being
re-scored, checks or AI review finishing, a teacher saving the board or rejecting, a new
Classroom version, an extra attempt. The unit is the student's assignment (the whole squad
for a squad), not one submission: requirements are independent, so a new upload must not
wipe a passed quiz or saved scores. Idempotent; never commits.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.logging import get_logger
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    SubjectsAssignment,
    Submission,
)
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus
from submissions_checker.db.models.squad import Squad
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import quiz_grants, requirements, squads
from submissions_checker.services.grading import squad_quiz_pct
from submissions_checker.services.notification_service import enqueue_teacher_review_notification

logger = get_logger(__name__)

REJECTED_KEY = "rejected"
S = SubmissionStatus
SETTLED = frozenset({S.PENDING, S.QUIZ_SENT, S.AWAITING_TEACHER_REVIEW, S.COMPLETED, S.FAILED})
PROCESSING = frozenset({S.VALIDATING, S.TESTING, S.AWAITING_AI_REVIEW, S.AI_REVIEWING})
CHECK_FAILED = frozenset({S.VALIDATION_FAILED, S.TEST_FAILED, S.AI_REVIEW_FAILED})
_EVENT_TARGET = {
    "requirements_met": S.COMPLETED,
    "requirement_failed": S.FAILED,
    "awaiting_teacher": S.AWAITING_TEACHER_REVIEW,
    "awaiting_quiz": S.QUIZ_SENT,
}


@dataclass(frozen=True)
class SettleResult:
    submission_id: int | None
    old_status: SubmissionStatus | None
    new_status: SubmissionStatus | None
    evaluation: requirements.Evaluation

    @property
    def completed_now(self) -> bool:
        return self.new_status == S.COMPLETED and self.old_status != S.COMPLETED


def plan_for(asg: SubjectsAssignment) -> requirements.Plan:
    return requirements.from_config(asg.config, span=asg.max_grade - asg.min_grade)


def _pct(score: float | None, max_score: float | None) -> float | None:
    return float(score) / float(max_score) * 100.0 if score is not None and max_score else None


async def _attempts(db: AsyncSession, sub_ids: list[int]) -> list[QuizAttempt]:
    if not sub_ids:
        return []
    rows = await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id.in_(sub_ids)))
    return list(rows.scalars().all())


async def quiz_host(db: AsyncSession, subs: list[Submission]) -> Submission | None:
    """The submission the student's quiz attempts hang off: the latest that has any,
    else the latest one. Attempt counts and grants are per submission, so a later upload
    must not reset them."""
    attempts = await _attempts(db, [s.id for s in subs])
    with_attempts = {a.submission_id for a in attempts}
    return next((s for s in subs if s.id in with_attempts), subs[0] if subs else None)


async def _quiz_fact(
    db: AsyncSession, subs: list[Submission], squad: Squad | None
) -> tuple[requirements.State, float | None]:
    attempts = await _attempts(db, [s.id for s in subs])
    for sub in subs:
        if await squads.quiz_complete(db, sub):
            if squad is not None:
                pct, _ = squad_quiz_pct([a for a in attempts if a.submission_id == sub.id])
                return "done", pct
            best = max(
                (p for a in attempts if a.is_passed and (p := _pct(a.score, a.max_score)) is not None),
                default=None,
            )
            return "done", best
    host = await quiz_host(db, subs)
    if host is not None:
        base = await quiz_grants.base_max_attempts(db, host)
        if base is not None:
            for sid in await quiz_grants._students_on(db, host):
                mine = [a for a in attempts if a.submission_id == host.id
                        and a.student_id in (sid, None)]
                if quiz_grants.is_exhausted(mine, quiz_grants.effective_max_attempts(base, host, sid)):
                    return "failed", None
    return "pending", None


def _code_facts(
    subs: list[Submission],
) -> tuple[tuple[requirements.State, float | None], tuple[requirements.State, float | None]]:
    code = next((s for s in subs if s.source_type == SubmissionSourceType.ZIP_UPLOAD), None)
    if code is None:
        return ("pending", None), ("pending", None)
    st = code.status
    tr = code.test_results or {}
    if st in (S.PENDING, S.VALIDATING, S.TESTING):
        return ("running", None), ("pending", None)
    if st in (S.VALIDATION_FAILED, S.TEST_FAILED):
        return ("failed", None), ("pending", None)
    tests_pct = _pct(tr.get("score"), tr.get("max_score"))
    tests: tuple[requirements.State, float | None] = (
        ("done", tests_pct) if tests_pct is not None else ("pending", None)
    )
    mark = (code.ai_review or {}).get("code_mark")
    if st in (S.AWAITING_AI_REVIEW, S.AI_REVIEWING):
        ai: tuple[requirements.State, float | None] = ("running", None)
    elif st == S.AI_REVIEW_FAILED:
        ai = ("failed", None)
    elif isinstance(mark, int | float) and not isinstance(mark, bool):
        ai = ("done", float(mark))
    else:
        ai = ("pending", None)
    return tests, ai


async def gather_facts(
    db: AsyncSession, sa: StudentAssignment, asg: SubjectsAssignment
) -> tuple[requirements.Facts, list[Submission], Squad | None, list[int]]:
    scope = await squads.scope_sa_ids(db, sa.student_id, asg.subject_id, asg.id) or [sa.id]
    subs = list(
        (
            await db.execute(
                select(Submission)
                .where(Submission.students_assignment_id.in_(scope))
                .order_by(Submission.created_at.desc(), Submission.id.desc())
            )
        ).scalars().all()
    )
    squad = await squads.active_squad(db, asg.subject_id, sa.student_id)
    tests, ai = _code_facts(subs)
    facts = requirements.Facts(
        quiz=await _quiz_fact(db, subs, squad),
        tests=tests,
        ai_review=ai,
        scores=dict(sa.teacher_scores or {}),
    )
    return facts, subs, squad, scope


async def _new_upload_less(
    db: AsyncSession, sa: StudentAssignment, asg: SubjectsAssignment, squad: Squad | None
) -> Submission:
    config_id = await db.scalar(
        select(SubjectPluginConfig.id)
        .where(SubjectPluginConfig.subject_id == asg.subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=S.PENDING,
        plugin_config_id=config_id,
        squad_id=squad.id if squad else None,
        test_results={"skipped": True, "reason": "requirements"},
    )
    db.add(sub)
    await db.flush()
    return sub


async def _write_grade(
    db: AsyncSession, latest: Submission, scope: list[int], squad: Squad | None,
    evaluation: requirements.Evaluation, subs: list[Submission],
) -> None:
    for sa_id in scope:
        row = await db.get(StudentAssignment, sa_id)
        if row is not None:
            row.grade = evaluation.grade
    breakdown = dict(evaluation.breakdown or {})
    if squad is not None:
        _, members = squad_quiz_pct(await _attempts(db, [s.id for s in subs]))
        breakdown["squad"] = {"unified": True, "squad_id": squad.id, "members": members}
    latest.grade_breakdown = breakdown


async def settle(db: AsyncSession, sa: StudentAssignment) -> SettleResult:
    """Grade and status of ``sa`` (and its squad) from what is done now. Does not commit."""
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None:
        raise ValueError(f"students_assignment {sa.id} has no assignment")
    plan = plan_for(asg)
    facts, subs, squad, scope = await gather_facts(db, sa, asg)
    evaluation = requirements.evaluate(
        plan, facts, asg.min_grade, asg.max_grade, round_up=squad is not None
    )
    if not subs:
        if not evaluation.complete:
            return SettleResult(None, None, None, evaluation)
        subs = [await _new_upload_less(db, sa, asg, squad)]
    latest = subs[0]
    await db.refresh(latest, attribute_names=["status", "source_metadata"], with_for_update=True)
    old = latest.status
    if old in PROCESSING or old in CHECK_FAILED or (
        old == S.PENDING and latest.source_type == SubmissionSourceType.ZIP_UPLOAD
    ):
        return SettleResult(latest.id, old, old, evaluation)
    if old == S.COMPLETED:
        if evaluation.complete:
            await _write_grade(db, latest, scope, squad, evaluation, subs)
        else:
            logger.warning("settle_completed_incomplete", submission_id=latest.id)
        return SettleResult(latest.id, old, old, evaluation)

    if (latest.source_metadata or {}).get(REJECTED_KEY) or evaluation.failed:
        event = "requirement_failed"
    elif evaluation.complete:
        event = "requirements_met"
    elif evaluation.pending_teacher() or not plan.has("quiz"):
        event = "awaiting_teacher"
    else:
        event = "awaiting_quiz"
    if _EVENT_TARGET[event] != old:
        transition(latest, event)
    if event == "requirements_met":
        await _write_grade(db, latest, scope, squad, evaluation, subs)
    if latest.status == S.AWAITING_TEACHER_REVIEW and old != S.AWAITING_TEACHER_REVIEW:
        await enqueue_teacher_review_notification(db, latest.id)
    logger.info("settled", submission_id=latest.id, old=str(old), new=str(latest.status),
                grade=evaluation.grade)
    return SettleResult(latest.id, old, latest.status, evaluation)
```

Fix the `enqueue_teacher_review_notification` import path to wherever `check_tasks.py` imports it from (`grep -n enqueue_teacher_review_notification src/submissions_checker/workers/tasks/check_tasks.py`). `quiz_grants._students_on` is private — rename it to `students_on` (update its 2 callers in `quiz_grants.py`) in this task.

- [ ] **Step 7: Run** `uv run --frozen --extra dev pytest tests/functional/test_settle.py tests/unit/test_state_machine.py -q` → PASS. Then `uv run --frozen mypy src/` → clean.

- [ ] **Step 8: Commit** — `Add settle(): grade and status from what is done` (body: one function replaces the per-mode completion branches; squad-safe via the submission row lock).

---

### Task 6: Quiz flows go through `settle`

**Files:**
- Modify: `src/submissions_checker/services/quiz_open.py`, `src/submissions_checker/api/routes/student_quiz.py:640-690` and `:745-790`, `src/submissions_checker/services/quiz_regrade.py:142-190`, `src/submissions_checker/services/quiz_grants.py:147-165`, `src/submissions_checker/api/routes/teacher_portal.py` (grant route: call settle after grant)
- Test: `tests/functional/test_quiz_and_teacher_scores.py`, `tests/functional/test_quiz_first_and_stepper.py`, `tests/functional/test_squad_quiz.py`, `tests/functional/test_quiz_disputes.py`, new cases in `tests/functional/test_settle.py`

**Interfaces:**
- Consumes: `settle.settle`, `settle.quiz_host`, `settle.gather_facts`, `settle.plan_for` (Task 5).
- Produces: `quiz_open.open_quiz_submission(db, sa, student_id) -> Submission` now opens for **any** assignment whose plan has a `quiz` item (not only scored mode), and returns the quiz host when one exists (reusing it unless its status is in `REOPENABLE`). Removes `is_scored_submission`, `scores_complete_for`.

- [ ] **Step 1: Failing tests** (append to `tests/functional/test_settle.py`)

```python
async def test_quiz_before_upload_on_upload_source(db, client, teacher, make_user, make_student):
    """source: upload with tests + quiz: the quiz opens before any ZIP exists."""
    cfg = {"max_grade": 16, "requirements": [
        {"type": "tests", "key": "tests", "points": 8},
        {"type": "quiz", "key": "quiz", "points": 8}]}
    subject, asg, student, sa = await arrange(db, teacher, make_student, config=cfg,
                                              with_submission=False)
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    r = await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz")
    assert r.status_code == 303 and "/portal/quiz/" in r.headers["location"]


async def test_quiz_pass_runs_settle(db, client, teacher, make_user, make_student):
    """Passing the last required item over HTTP completes the work."""
    subject, asg, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                                   scores={"report": 4, "accepted": 1})
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    attempt_id = await start_and_answer_correctly(client, subject.id, sa.id)  # helper below
    r = await client.post(f"/portal/quiz/{attempt_id}/submit")
    assert r.status_code in (200, 303)
    await db.refresh(sa)
    assert sa.grade == 12  # 8 + 4 + 0
```

Add `start_and_answer_correctly(client, subject_id, sa_id) -> int` to `_scored_helpers.py`, modelled on how `test_quiz_and_teacher_scores.py` drives start → answer → submit (grep its `"/answer"` calls). Check `make_user`'s signature in `tests/functional/conftest.py:201` for how a student user is linked.

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/functional/test_settle.py -q -k "quiz_before_upload or quiz_pass_runs"` → FAIL (403 "Quiz not available" / grade None).

- [ ] **Step 3: Implement**

`quiz_open.open_quiz_submission` — replace the mode check and reuse rule:

```python
    existing = await settle.quiz_host(db, await _scope_subs(db, sa))
    if existing is not None and existing.status not in REOPENABLE:
        return existing
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None or not settle.plan_for(asg).has("quiz"):
        raise QuizOpenError("Quiz not available for this submission")
    # … unchanged squad / config lookup …
    sub = Submission(..., test_results={"skipped": True, "reason": "requirements"})
    db.add(sub)
    await db.flush()
    if squad:
        squads.lock_on_submit(squad)
    transition(sub, "awaiting_quiz")  # PENDING → QUIZ_SENT; settle refines it later
    return sub
```

with `_scope_subs(db, sa)` = the same query `settle.gather_facts` uses (expose it from `settle.py` as `scope_submissions(db, sa, asg) -> tuple[list[Submission], list[int]]` and call it from both). Delete `is_scored_submission` and `scores_complete_for`.

`student_quiz.start_or_resume_quiz` (`:765-778`) — replace the gate:

```python
    asg = sa.subjects_assignment
    plan = settle.plan_for(asg)
    if not plan.has("quiz"):
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")
    facts, _subs, _squad, _scope = await settle.gather_facts(db, sa, asg)
    if facts.quiz[0] in ("done", "failed"):
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")
    try:
        latest_sub = await quiz_open.open_quiz_submission(db, sa, student_id)
    except quiz_open.QuizOpenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if latest_sub.status == SubmissionStatus.COMPLETED:
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")
```

(`done` for a squad means every member passed; a member who passed while a mate has not is stopped by the existing per-student "already passed" logic further down — keep it.)

`student_quiz` outcome block (`:643-690`) — replace the whole `if submission and submission.status == QUIZ_SENT:` block with:

```python
    if submission is not None:
        await db.flush()  # settle must see this attempt's is_passed / status
        if not is_passed:
            max_attempts = effective_max_attempts(
                attempt.config_snapshot.get("max_quiz_attempts"), submission, attempt.student_id
            )
            if max_attempts is not None:
                prior = await _count_used_attempts(
                    db, attempt.submission_id, attempt.id, attempt.student_id
                )
                attempts_left = max(max_attempts - (prior + 1), 0)
        owner_sa = await db.get(StudentAssignment, submission.students_assignment_id)
        if owner_sa is not None:
            result = await settle.settle(db, owner_sa)
            if is_passed and submission.squad_id is not None and not result.evaluation.complete:
                await _notify_partners_passed(db, submission, attempt.student_id)
```

`quiz_regrade._advance_submission` — replace the body after the `None` check with:

```python
    sa = await db.get(StudentAssignment, submission.students_assignment_id)
    if sa is not None:
        await settle.settle(db, sa)
```

`quiz_grants.grant_extra_attempt` — keep `transition(submission, "quiz_attempt_granted")`; the teacher route `teacher_grant_quiz_attempt` then calls `await settle.settle(db, own_sa_row)` before commit (status may move to AWAITING_TEACHER_REVIEW when teacher items are pending; the gate above only looks at facts, so the student can start).

- [ ] **Step 4: Fix the existing quiz tests.** Run `uv run --frozen --extra dev pytest tests/functional/test_quiz_and_teacher_scores.py tests/functional/test_quiz_first_and_stepper.py tests/functional/test_squad_quiz.py tests/functional/test_quiz_disputes.py tests/functional/test_teacher_grant_quiz_attempt.py tests/unit/test_quiz_timing.py -q`. Expected differences, and how to update each (assertions change, behaviour under test stays):
  - status after a pass in `quiz_then_teacher` / scored mode without scores: still `AWAITING_TEACHER_REVIEW` ✓.
  - status after the last failed attempt: `FAILED` ✓ (now via settle).
  - `quiz_only` pass: `COMPLETED` ✓.
  - grade_breakdown assertions on `mode == "quiz_and_teacher_scores"`/`"criteria"` → assert `mode == "requirements"` and the `items` list instead.
  - tests asserting a quiz cannot start from `AWAITING_TEACHER_REVIEW` → the new rule allows it while the quiz item is pending; change to assert it starts, and keep a test that it refuses once passed.
  Any other failure is a bug in this task — fix the code, not the test.

- [ ] **Step 5: Run** the same command → PASS; `uv run --frozen mypy src/` clean.

- [ ] **Step 6: Commit** — `Route quiz outcomes through settle` (body: the quiz is one independent item; it opens whenever it is pending, before or after any upload).

---

### Task 7: Workers go through `settle`

**Files:**
- Modify: `src/submissions_checker/workers/tasks/check_tasks.py` (`_QUIZ_FIRST_MODES`, `is_quiz_first_assignment`, `_execute_check`, `_accept_without_checks`, `_advance_after_tests`), `src/submissions_checker/workers/tasks/review_tasks.py:300-322`, `src/submissions_checker/api/routes/student_portal.py:58,678`, `src/submissions_checker/api/routes/teacher_portal.py` (`_next_step_for`, `teacher_retry_ai_review`, `teacher_skip_ai_review`), `templates/teacher_assignment.html` (drop the send-to-teacher button)
- Test: `tests/integration/test_worker_tasks.py`, `tests/functional/test_ai_review_flow.py`, `tests/unit/test_check_task_delegation.py`, `tests/functional/test_teacher_portal.py`

**Interfaces:**
- Consumes: `settle.settle`, `settle.plan_for`.
- Produces: `check_tasks.accepts_without_checks(db, subject_id, assignment_code) -> bool` (renamed from `is_quiz_first_assignment`): true when the latest config's assignment plan has `source == "upload"` and no `tests`/`ai_review` item. RUN_AI_REVIEW payload is `{"submission_id": id}` only.

- [ ] **Step 1: Failing tests** (add to `tests/integration/test_worker_tasks.py`, reusing its existing arrange for a tests-only submission and its fake sandbox)

```python
async def test_tests_then_teacher_waits_for_the_check(db_session, ...) -> None:
    """Translated tests_then_teacher: passing tests leaves the work with the teacher."""
    sub = await arrange_zip_submission(db_session, review_mode="tests_then_teacher")
    await execute_check_task(db_session, {"submission_id": sub.id})
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert sub.students_assignment.grade is None


async def test_tests_only_completes_with_grade(db_session, ...) -> None:
    sub = await arrange_zip_submission(db_session, review_mode="tests_only")
    await execute_check_task(db_session, {"submission_id": sub.id})
    assert sub.status == SubmissionStatus.COMPLETED
    assert sub.students_assignment.grade is not None
```

(Name the arrange after whatever helper the file already uses; the point is one translated mode that stops with the teacher and one that completes.)

- [ ] **Step 2: Run** `uv run --frozen --extra dev --with redis pytest tests/integration/test_worker_tasks.py -q` → the new tests FAIL where behaviour differs (`tests_then_teacher` currently passes too via the old branch — if both pass already, keep them as regression guards and move on).

- [ ] **Step 3: Implement**

In `check_tasks.py`:

```python
async def accepts_without_checks(
    db: AsyncSession, subject_id: int, assignment_code: str | None
) -> bool:
    """True when the assignment takes an upload but runs no checker (no tests/ai items)."""
    if not assignment_code:
        return False
    asg = await db.scalar(
        select(SubjectsAssignment).where(
            SubjectsAssignment.subject_id == subject_id, SubjectsAssignment.code == assignment_code
        )
    )
    if asg is None:
        return False
    plan = settle.plan_for(asg)
    return plan.source == "upload" and not (plan.has("tests") or plan.has("ai_review"))
```

In `_execute_check`, replace `review_mode` / `_QUIZ_FIRST_MODES` with:

```python
    plan = settle.plan_for(subjects_assignment)
    if not (plan.has("tests") or plan.has("ai_review")):
        await _accept_without_checks(db, submission)
        return
```

`_accept_without_checks(db, submission)` ends with:

```python
    submission.test_results = {"skipped": True, "reason": "requirements"}
    transition(submission, "start_validation")
    transition(submission, "validation_passed")
    transition(submission, "processing_done")
    await settle.settle(db, submission.students_assignment)
```

After `transition(submission, "test_failed")` add `await settle.settle(db, student_assignment)` (no status change; keeps one code path). Replace `_advance_after_tests` with:

```python
async def _advance_after_tests(db: AsyncSession, submission: Submission) -> None:
    sa = submission.students_assignment
    if settle.plan_for(sa.subjects_assignment).has("ai_review"):
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(event_type=OutboxEventType.RUN_AI_REVIEW,
                             state=OutboxMessageState.PENDING,
                             payload={"submission_id": submission.id}))
        return
    transition(submission, "processing_done")
    result = await settle.settle(db, sa)
    if result.completed_now:
        asg = sa.subjects_assignment
        await _notify_student(
            db, sa.student_id, title=f"{asg.title}: passed",
            body=f'Your submission for "{asg.title}" passed the automated checks.',
            link=f"/portal/subjects/{asg.subject_id}/assignments/{asg.id}",
        )
```

Delete `_QUIZ_FIRST_MODES`, `is_quiz_first_assignment`, the `finalize_grade` import. In `student_portal.py` switch the import + call to `accepts_without_checks`; also replace the scored-mode upload refusal at `:558` with `if settle.plan_for(subjects_assignment).source != "upload": raise HTTPException(409, "This assignment takes no upload")`.

In `review_tasks.py` replace the `next_step` branch block with:

```python
    transition(submission, "processing_done")
    await settle.settle(db, submission.students_assignment)
```

(make sure `students_assignment` is loaded — follow the `selectinload` the task already uses; drop the `next_step` variable and the `is_flagged` reroute — a flagged verdict still shows its badge on the board, and the teacher can Reject.)

In `teacher_portal.py`: delete `_next_step_for`; retry payload becomes `{"submission_id": submission.id}`; delete `teacher_skip_ai_review` and its `send-to-teacher` button in `templates/teacher_assignment.html`.

- [ ] **Step 4: Update affected tests.** Run `uv run --frozen --extra dev --with redis pytest tests/integration/test_worker_tasks.py tests/functional/test_ai_review_flow.py tests/unit/test_check_task_delegation.py tests/functional/test_teacher_portal.py tests/functional/test_squad_submission.py -q`. Expected changes: payload `next_step` assertions → removed; the flagged-verdict-goes-to-teacher test in `tests_then_ai_then_quiz` → now asserts QUIZ_SENT plus the board flag badge; the send-to-teacher route test → assert 404/405. Anything else is a code bug.

- [ ] **Step 5: Run** the same → PASS. **Step 6: Commit** — `Finish checks and AI review through settle` (body: the workers no longer know review modes; flagged verdicts are shown, not rerouted).

---

### Task 8: Config apply and LLM config read requirements

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py` (`_validate_quiz_reachability`, `_validate_teacher_scores`, `_diff_assignment.config_keys`, `_build_assignment_config`), `src/submissions_checker/services/llm_grading/config.py`
- Test: `tests/integration/test_config_apply.py`, `tests/unit/test_llm_grading_config.py`, `tests/functional/test_classroom_config_apply.py`

**Interfaces:**
- Consumes: `requirements.validate`, `requirements.from_config`, `RequirementsError`.
- Produces:
  - `llm_config.is_llm_graded(cfg) -> bool`: `(cfg.get("llm_grading") or {}).get("enabled") is True and bool(requirements.from_config(cfg).llm_items())`.
  - `llm_config.llm_criteria(assignment_config) -> list[LLMCriterion]` — **signature change**: takes the whole assignment config (was `grading_cfg`); built from `plan.llm_items()` (`LLMCriterion(key, title, int(points), llm)`). Update callers: `runner.build_request`, `teacher_classroom.py:806`, `board.py` (grep `llm_criteria(`).
  - `llm_config.validate`: drop the scored-mode requirement; require `plan.source == "classroom"`; "needs at least one criterion" → "needs at least one score item with llm".

- [ ] **Step 1: Failing tests** (append to `tests/integration/test_config_apply.py`, using the file's existing zip-building helper)

```python
NEW_STYLE = {
    "min_grade": 0, "max_grade": 16, "source": "classroom",
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5, "llm": "rubric"},
        {"type": "score", "key": "star", "title": "Зірочка", "max": 3, "optional": True},
        {"type": "check", "key": "accepted", "title": "Прийнято викладачем", "points": 0},
    ],
    "quiz": QUIZ_BLOCK,
}


async def test_new_style_assignment_applies_and_is_stored(db_session, ...) -> None:
    result = await apply(db_session, {"assignments": {"lab4": NEW_STYLE}})
    asg = await assignment_by_code(db_session, "lab4")
    assert asg.config["requirements"][3]["key"] == "accepted"
    assert asg.config["source"] == "classroom"


@pytest.mark.parametrize("bad", [
    {**NEW_STYLE, "review_mode": "quiz_and_teacher_scores"},
    {**NEW_STYLE, "max_grade": 17},
    {**NEW_STYLE, "requirements": [*NEW_STYLE["requirements"],
                                   {"type": "check", "key": "x", "title": "X", "max": 1}]},
    {**NEW_STYLE, "requirements": [*NEW_STYLE["requirements"],
                                   {"type": "tests", "key": "t", "points": 0}]},
])
async def test_bad_new_style_refused(db_session, bad, ...) -> None:
    with pytest.raises(ValueError):
        await apply(db_session, {"assignments": {"lab4": bad}})


async def test_v7_style_scored_config_still_applies(db_session, ...) -> None:
    """Review Focus 5: re-applying an old scored config after v8 still validates."""
    await apply(db_session, {"assignments": {"lab4": V7_SCORED}})  # existing fixture shape
```

and in `tests/unit/test_llm_grading_config.py`:

```python
def test_new_style_llm_graded() -> None:
    cfg = {"source": "classroom", "llm_grading": {"enabled": True}, "max_grade": 5,
           "requirements": [{"type": "score", "key": "r", "title": "R", "max": 5, "llm": "x"}]}
    assert is_llm_graded(cfg)
    assert [c.key for c in llm_criteria(cfg)] == ["r"]
```

- [ ] **Step 2: Run** → FAIL (`requirements` not copied; review_mode+requirements accepted).

- [ ] **Step 3: Implement**
  - `_validate_quiz_reachability`: skip assignments that have `requirements` (their validation covers quiz reachability).
  - `_validate_teacher_scores` → rename `_validate_requirements`:

```python
    def _validate_requirements(self, new_cfg: dict[str, Any]) -> None:
        """New-style lists must add up; old scored configs keep their own check."""
        for code, a_cfg in (new_cfg.get("assignments") or {}).items():
            a_cfg = a_cfg or {}
            has_quiz = bool((a_cfg.get("quiz") or {}).get("questions"))
            if "requirements" in a_cfg:
                requirements.validate(code, a_cfg, has_quiz_block=has_quiz)
            elif teacher_scores.is_scored_mode(a_cfg):
                teacher_scores.validate_assignment(code, a_cfg)
```

  (update the call site name; `RequirementsError` is a `ValueError`, which the apply route already turns into a 422 flash.)
  - Add `"requirements"` and `"source"` to both key tuples (`config_keys` in `_diff_assignment` and `_build_assignment_config`).
  - Rewrite `llm_grading/config.py` per Interfaces.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/integration/test_config_apply.py tests/integration/test_config_apply_edges.py tests/unit/test_llm_grading_config.py tests/unit/test_config_apply_helpers.py tests/functional/test_apply_config.py tests/functional/test_classroom_config_apply.py tests/functional/test_classroom_board.py tests/functional/test_classroom_report.py -q` → PASS (update `llm_criteria(grading_cfg)` call sites in tests to pass the assignment config).

- [ ] **Step 5: Commit** — `Accept requirements lists in subject configs`.

---

### Task 9: Board — item columns, locked check, batch save, no approve

**Files:**
- Create: `src/submissions_checker/services/board_scores.py`
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (board ctx ~`:585-740`, `_apply_review_decision` ~`:1546`, review route, bulk route, `teacher_save_scores` ~`:1937`), `templates/teacher_assignment.html`, `templates/teacher_submission_review.html:165-180`, `i18n/uk.yml`
- Test: `tests/functional/test_board_batch.py` (new), `tests/functional/test_quiz_and_teacher_scores.py`, `tests/functional/test_teacher_portal.py`, `tests/functional/test_classroom_board.py`

**Interfaces:**
- Consumes: `requirements.parse_scores_form`, `row_signature`, `ScoreError`; `settle.settle`, `settle.plan_for`, `settle.REJECTED_KEY`; `llm_board_state` (existing in `services/llm_grading/board.py`).
- Produces:
  - `class RowError(Exception): code: str` with codes `not_enrolled | confirm_student | draft_changed | invalid | completed_required`, `detail: str`.
  - `async def save_row(db, assignment: SubjectsAssignment, student_id: int, form: Mapping[str, str], *, prefix: str, llm_grading_id: str | None, actor: TeacherUser) -> settle.SettleResult` — the old route body: enrolment check, LLM link-confirm + stale-draft guard, `parse_scores_form(plan, form, prefix=prefix, locked=old)`, refuse a removed required score on COMPLETED (`completed_required`), fan-out to squad rows, approve draft, audit `teacher_scores_set`, `settle`, and when `result.completed_now` add the `SUBMISSION_REVIEWED` approve outbox message.
  - `def board_values(plan, scores: Mapping[str, int]) -> dict[str, str]` — the row's rendered values: score → `str(v)` or `""`, check → `"1"` or `""`. The template's `orig__<sid>` = `row_signature(board_values(...))`.
  - Route `POST /teacher/subjects/{subject_id}/assignments/{sa_id}/scores/batch` → redirect `…?saved=N&skipped=<sid>:<code>,…`.
  - `POST /teacher/subjects/{id}/assignments/{sa}/scores` keeps working (prefix `score_`, field `llm_grading_id`), raising HTTP 404/409/422 from `RowError` codes as before.
  - `_apply_review_decision(db, submission, action, reason, user)` accepts only `reject`; approve → `HTTPException(400, "Invalid action")`. Reject: allowed when the latest submission is in `QUIZ_SENT` or `AWAITING_TEACHER_REVIEW`; sets `source_metadata[REJECTED_KEY] = {"reason", "by": user.user_id, "at": iso}` (reassign the dict), `test_results = {**(test_results or {}), "check_reason": reason}`, then `settle`.

- [ ] **Step 1: Failing tests** — `tests/functional/test_board_batch.py`

```python
"""Board batch save: changed rows only, partial failure, locked acceptance."""

from __future__ import annotations

import pytest

from submissions_checker.db.models import StudentAssignment
from submissions_checker.services import board_scores, requirements as rq
from tests.functional._scored_helpers import CFG, arrange, arrange_second_student, attempt

pytestmark = pytest.mark.asyncio


def _orig(scores):
    plan = rq.parse({"max_grade": 13, **CFG})
    return rq.row_signature(board_values(plan, scores))


async def test_batch_saves_changed_rows_only(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    s2, sa2 = await arrange_second_student(db, subject, asg, make_student, scores={"report": 3})
    form = {
        f"orig__{s1.id}": _orig({}), f"s{s1.id}__report": "4", f"s{s1.id}__accepted": "on",
        f"orig__{s2.id}": _orig({"report": 3}), f"s{s2.id}__report": "3",
    }
    r = await teacher_client.post(f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores/batch",
                                  data=form)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    await db.refresh(sa1)
    assert sa1.teacher_scores == {"report": 4, "accepted": 1}


async def test_bad_row_does_not_roll_back_others(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG)
    s2, sa2 = await arrange_second_student(db, subject, asg, make_student)
    form = {
        f"orig__{s1.id}": _orig({}), f"s{s1.id}__report": "9",  # above max
        f"orig__{s2.id}": _orig({}), f"s{s2.id}__report": "2",
    }
    r = await teacher_client.post(f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores/batch",
                                  data=form)
    loc = r.headers["location"]
    assert "saved=1" in loc and f"{s1.id}:invalid" in loc
    await db.refresh(sa2)
    assert sa2.teacher_scores == {"report": 2}


async def test_stale_tab_cannot_clear_saved_check(db, teacher_client, teacher, make_student):
    """Review Focus 2."""
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG,
                                             scores={"report": 4, "accepted": 1})
    form = {f"orig__{s1.id}": _orig({"report": 4}), f"s{s1.id}__report": "5"}  # no accepted
    await teacher_client.post(f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores/batch",
                              data=form)
    await db.refresh(sa1)
    assert sa1.teacher_scores == {"report": 5, "accepted": 1}


async def test_saved_check_renders_without_input(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, _ = await arrange(db, teacher, make_student, config=CFG,
                                             scores={"accepted": 1})
    html = (await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")).text
    assert f'name="s{s1.id}__accepted"' not in html
    assert "data-check-done" in html


async def test_check_and_quiz_complete_the_work(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, s1.id, score=4, max_score=4, passed=True)
    form = {f"orig__{s1.id}": _orig({}), f"s{s1.id}__report": "5", f"s{s1.id}__accepted": "on"}
    await teacher_client.post(f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores/batch",
                              data=form)
    await db.refresh(sa1)
    assert sa1.grade == 13


async def test_approve_is_gone(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(db, teacher, make_student, config=CFG,
                                               status=SubmissionStatus.AWAITING_TEACHER_REVIEW)
    r = await teacher_client.post(f"/teacher/submissions/{sub.id}/review", data={"action": "approve"})
    assert r.status_code == 400
    r = await teacher_client.post(f"/teacher/subjects/{subject.id}/assignments/{asg.id}/bulk",
                                  data={"action": "approve", "submission_ids": [sub.id]})
    assert r.status_code == 400
    html = (await teacher_client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")).text
    assert 'value="approve"' not in html


async def test_reject_fails_with_reason(db, teacher_client, teacher, make_student):
    subject, asg, s1, sa1, sub = await arrange(db, teacher, make_student, config=CFG,
                                               status=SubmissionStatus.AWAITING_TEACHER_REVIEW)
    r = await teacher_client.post(f"/teacher/submissions/{sub.id}/review",
                                  data={"action": "reject", "reason": "копія"})
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED
    assert sub.source_metadata["rejected"]["reason"] == "копія"
```

Add an LLM-board case for Review Focus 3 in `tests/functional/test_classroom_board.py` using that file's existing arrange (draft id posted ≠ current draft id → that row's code is `draft_changed`, the other row saved).

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/functional/test_board_batch.py -q` → FAIL (404 on `/scores/batch`).

- [ ] **Step 3: Implement `services/board_scores.py`** — move the body of `teacher_save_scores` (`teacher_portal.py:1955-2073`) into `save_row` with these changes: `crits`/`teacher_scores.parse_form` → `plan = settle.plan_for(assignment)`, `new_scores = requirements.parse_scores_form(plan, form, prefix=prefix, locked=old)` where `old = dict(own.teacher_scores or {})` (read **before** parsing; `own` is loaded `with_for_update` as now); the "graded work cannot lose a required score" check becomes:

```python
    if submission is not None and submission.status == SubmissionStatus.COMPLETED:
        lost = [i.key for i in plan.teacher_items()
                if not i.optional and i.key in old and i.key not in new_scores]
        if lost:
            raise RowError("completed_required", "A graded work cannot lose a required score")
```

the completion block becomes:

```python
    await db.flush()
    result = await settle.settle(db, own)
    if result.completed_now and result.submission_id is not None:
        db.add(OutboxMessage(event_type=OutboxEventType.SUBMISSION_REVIEWED,
                             state=OutboxMessageState.PENDING,
                             payload={"submission_id": result.submission_id,
                                      "action": "approve", "reason": ""}))
    return result
```

HTTP mapping in the single route: `not_enrolled → 404`, `confirm_student`/`draft_changed → 409`, `invalid`/`completed_required → 422`.

Batch route:

```python
@router.post("/subjects/{subject_id}/assignments/{sa_id}/scores/batch")
async def teacher_save_scores_batch(
    request: Request, subject_id: int, sa_id: int, db: DBSession, current_user: TeacherUser
) -> RedirectResponse:
    """Save every changed board row; a failing row is reported, the others still save."""
    await require_subject_access(db, subject_id, current_user)
    assignment = await db.get(SubjectsAssignment, sa_id)
    if assignment is None or assignment.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Assignment not found")
    plan = settle.plan_for(assignment)
    form = {k: str(v) for k, v in (await request.form()).items()}
    saved, skipped = 0, []
    for name, orig in form.items():
        if not name.startswith("orig__") or not name[6:].isdecimal():
            continue
        sid = int(name[6:])
        prefix = f"s{sid}__"
        row = {k[len(prefix):]: v.strip() for k, v in form.items() if k.startswith(prefix)}
        rendered = {i.key: row.get(i.key, "") for i in plan.teacher_items()}
        rendered = {k: ("1" if v in ("1", "on") else v) for k, v in rendered.items()}
        if requirements.row_signature(rendered) == orig:
            continue
        try:
            async with db.begin_nested():
                await board_scores.save_row(
                    db, assignment, sid, form, prefix=prefix,
                    llm_grading_id=form.get(f"llm_grading_id__{sid}"), actor=current_user,
                )
            saved += 1
        except board_scores.RowError as exc:
            skipped.append(f"{sid}:{exc.code}")
    await db.commit()
    query = f"saved={saved}" + (f"&skipped={','.join(skipped)}" if skipped else "")
    return RedirectResponse(
        url=f"/teacher/subjects/{subject_id}/assignments/{sa_id}?{query}", status_code=303
    )
```

Note on `rendered`: a check already saved renders no input, so its posted value is `""` and its `board_values` entry is `"1"` — compute `board_values` for the orig from the **saved** scores, and for the posted side treat a locked check as `"1"`: load the row's saved checks inside the loop (`own.teacher_scores`) and set `rendered[k] = "1"` for each saved check before hashing. Put this in a helper `posted_values(plan, row, saved) -> dict[str, str]` in `board_scores.py` with a unit test in `tests/unit/test_requirements_form.py`.

Board context (`teacher_portal.py` board handler): replace `scored`/`criteria`/`quiz_cells` with:
- `plan = settle.plan_for(assignment)`, `items = list(plan.items)`, `teacher_items = plan.teacher_items()`;
- per row `evals[student_id] = (await settle.gather_facts(...))` → `requirements.evaluate(...)` — gather once per row; rows with no SA row get `Facts()`;
- per row `orig[student_id] = requirements.row_signature(board_scores.board_values(plan, row.teacher_scores or {}))`;
- `batch_result` from `saved`/`skipped` query params, mapping each `sid:code` to `(full_name, vocab.teacher.batch_reason_<code>)`;
- keep `quiz_cells` for the attempts-used text of a pending quiz (rename the dict key it reads from `teacher_scores.quiz_points_for` to the evaluation's quiz item points).

Template `teacher_assignment.html`:
- remove the bulk **approve** button (`:102-103`) and the row `review_action` link's approve path stays (review page now shows reject only);
- header: one `<th>` per `items` entry (`{{ i.title }} /{{ i.points|int }}` for score/quiz/tests/ai; check shows title only), then total;
- cells:
  - quiz/tests/ai_review: badge from `evals[row.student_id].items[loop.index0]` — `done` → `points|round(1)`/max ✓, `running` → ⏳, `failed` → ✗, `pending` → `—` (quiz pending shows `used/cap` from `quiz_cells`);
  - score: `<input form="batch-form" type="number" name="s{{ sid }}__{{ i.key }}" value="{{ saved or '' }}" placeholder="{{ ai or '' }}" data-ai-draft="{{ ai or '' }}" …>` (AI draft is a placeholder, never a value);
  - check: saved → `<span data-check-done class="text-green-700 text-xs">✓ {{ i.title }}</span>` and **no input**; not saved → `<input form="batch-form" type="checkbox" name="s{{ sid }}__{{ i.key }}" value="1">`;
  - per row hidden `orig__{{ sid }}` and (LLM) `llm_grading_id__{{ sid }}`, both `form="batch-form"`.
- after `#bulk-form`, add `<form id="batch-form" method="POST" action="…/scores/batch" class="hidden"></form>` and, above the table, two buttons with `form="batch-form"`: **Зберегти зміни** (`type="submit"`) and **Взяти бали з AI-чернетки** (`type="button"` with inline JS: for each checked `#bulk-form input[name=submission_ids]`, find its row's `input[data-ai-draft]` and set `value = dataset.aiDraft` when non-empty). Remove the per-row Save buttons and the per-row `scores-<sid>` forms.
- filter: a `<select>` "Не виконано: <item>" that hides rows (`data-pending-keys="report accepted"` on each `<tr>`, small inline JS toggling `hidden`).
- flash: when `batch_result`, show "Збережено: N" and a list "Пропущено: <name> — <reason>".

`teacher_submission_review.html:165-180`: delete the approve button and its JS branch.

`i18n/uk.yml` under `teacher:` add: `save_changes: "Зберегти зміни"`, `take_ai_draft: "Взяти бали з AI-чернетки"`, `batch_saved: "Збережено"`, `batch_skipped: "Пропущено"`, `batch_reason_not_enrolled: "студент не записаний"`, `batch_reason_confirm_student: "спершу підтвердьте студента"`, `batch_reason_draft_changed: "чернетка змінилась — оновіть сторінку"`, `batch_reason_invalid: "некоректний бал"`, `batch_reason_completed_required: "не можна прибрати обов'язковий бал у зарахованій роботі"`, `filter_not_done: "Не виконано"`, `check_done: "Прийнято"`. Remove `bulk_approve_selected` and `approve` once no template references them.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/functional/test_board_batch.py tests/functional/test_quiz_and_teacher_scores.py tests/functional/test_teacher_portal.py tests/functional/test_classroom_board.py tests/unit/test_requirements_form.py -q`. Update old tests: per-row `scores` route tests keep passing (route kept); approve tests → assert 400; scored-mode "reject is refused" tests → assert reject now works. → PASS.

- [ ] **Step 5: Commit** — `Batch-save the board and make acceptance a locked check` (body: acceptance is a requirement, not a workflow action; 40 form posts become one; a saved check cannot be unticked).

---

### Task 10: Student checklist and upload by source

**Files:**
- Modify: `src/submissions_checker/api/routes/student_portal.py` (detail handler ~`:340-500`), `src/submissions_checker/api/schemas/student_portal.py`, `templates/assignment_detail.html` (`:148-230`, `:270-320`), `i18n/uk.yml`
- Test: `tests/functional/test_portal_detail_pages.py`, `tests/functional/test_classroom_student_view.py`

**Interfaces:**
- Consumes: `settle.gather_facts`, `settle.plan_for`, `requirements.evaluate`.
- Produces: `@dataclass ChecklistItem: key: str; title: str; type: str; state: str; text: str; optional: bool` in `schemas/student_portal.py`; `AssignmentDetail.checklist: list[ChecklistItem]`, `.quiz_available: bool`, `.source: str | None`.

`text` rules (never leak drafts): quiz done → `"{points:g}/{max:g}"`; quiz pending/failed → `vocab.portal.checklist_quiz_pending`/`checklist_quiz_failed`; score done → `"{v}/{max}"`, pending → `vocab.portal.checklist_waiting_teacher`; check done → `"✓"`, pending → `vocab.portal.checklist_defend`; tests/ai like quiz with `checklist_checking` for running.

- [ ] **Step 1: Failing tests** (append to `tests/functional/test_portal_detail_pages.py`)

```python
async def test_checklist_shows_items_and_no_draft(db, client, teacher, make_user, make_student):
    subject, asg, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                                   scores={"report": 4})
    await add_llm_draft(db, asg, student, points={"report": 5})  # helper from classroom tests
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    html = (await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")).text
    assert "data-checklist" in html
    assert "4/5" in html and "5/5" not in html
    assert "Прийнято викладачем" in html
    assert 'id="upload-form"' not in html  # source: classroom


async def test_quiz_button_while_teacher_items_pending(db, client, teacher, make_user, make_student):
    subject, asg, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                                   status=SubmissionStatus.AWAITING_TEACHER_REVIEW)
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    html = (await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")).text
    assert f"/assignments/{sa.id}/quiz" in html


async def test_upload_form_for_upload_source(db, client, teacher, make_user, make_student):
    cfg = {"max_grade": 10, "requirements": [{"type": "tests", "key": "tests", "points": 10}]}
    subject, asg, student, sa = await arrange(db, teacher, make_student, config=cfg,
                                              with_submission=False)
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    html = (await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")).text
    assert 'id="upload-form"' in html
```

(`add_llm_draft`: grep `tests/functional/test_classroom_student_view.py` for how it creates a `ClassroomWork` + `LLMGrading` with a draft; lift it into `_scored_helpers.py`.)

- [ ] **Step 2: Run** → FAIL (no `data-checklist`).

- [ ] **Step 3: Implement** — in the detail handler, after `latest_sub` is known:

```python
    asg = sa.subjects_assignment
    plan = settle.plan_for(asg)
    facts, _subs, squad, _scope = await settle.gather_facts(db, sa, asg)
    evaluation = requirements.evaluate(plan, facts, asg.min_grade, asg.max_grade,
                                       round_up=squad is not None)
    checklist = [_checklist_item(i, sa.teacher_scores or {}, vocab) for i in evaluation.items]
    quiz_available = plan.has("quiz") and facts.quiz[0] == "pending" and attempts_left_ok
```

where `attempts_left_ok` reuses the existing `quiz_max_attempts`/`quiz_attempts_used` numbers (`quiz_max_attempts is None or used < quiz_max_attempts`) and a squad member who already passed gets `False` (existing `squad_panel.my_passed`). Replace `scored = teacher_scores.is_scored_mode(...)` uses: breakdown is always shown when `grade_breakdown` exists and `mode == "requirements"`; the max-attempts fallback reads the latest config whenever `latest_sub is None` (not only in scored mode).

Template: a new card at the top of the status area:

```jinja
<div class="bg-white rounded-xl border border-slate-200 shadow-sm p-5 mb-5" data-checklist>
  <div class="flex justify-between mb-3">
    <h2 class="font-semibold text-slate-900">{{ vocab.portal.checklist_title }}</h2>
    <span class="text-sm">{{ vocab.portal.grade_label }}:
      {% if assignment.grade is not none %}<strong>{{ assignment.grade }}</strong>/{{ assignment.max_grade }}
      {% else %}— <span class="text-slate-400">({{ vocab.portal.checklist_grade_hint }})</span>{% endif %}</span>
  </div>
  <ul class="space-y-1.5 text-sm">
    {% for c in assignment.checklist %}
    <li class="flex justify-between">
      <span>{% if c.state == "done" %}✓{% elif c.state == "failed" %}✗{% elif c.state == "running" %}⏳{% else %}○{% endif %}
        {{ c.title }}{% if c.optional %} <span class="text-slate-400">({{ vocab.portal.optional }})</span>{% endif %}</span>
      <span class="text-slate-500">{{ c.text }}</span>
    </li>
    {% endfor %}
  </ul>
  {% if assignment.quiz_available %}
  <a href="/portal/subjects/{{ subject_id }}/assignments/{{ assignment.student_assignment_id }}/quiz"
     class="mt-4 inline-flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium px-4 py-2 rounded-lg">{{ vocab.quiz.start_quiz }}</a>
  {% endif %}
  {% if assignment.source == "classroom" %}
  <p class="mt-3 text-sm text-slate-500">{{ vocab.portal.checklist_hand_in_classroom }}</p>
  {% endif %}
</div>
```

Gate the old scored panel (`:217-230`) and the old QUIZ_SENT quiz button (`:272-289`) off (delete them; the checklist card replaces both, including the squad slice label — move the `assignment.squad.my_slice` button text into the checklist's quiz button). Gate the upload form on `assignment.source == "upload"` instead of `not scored`.

`i18n/uk.yml` `portal:` add `checklist_title: "Що потрібно для зарахування"`, `checklist_grade_hint: "з'явиться, коли все виконано"`, `checklist_waiting_teacher: "очікує оцінки викладача"`, `checklist_defend: "захисти роботу викладачу"`, `checklist_quiz_pending: "ще не складено"`, `checklist_quiz_failed: "спроби вичерпано"`, `checklist_checking: "перевіряється"`, `checklist_hand_in_classroom: "Здавай роботу в Google Classroom."`, `optional: "необов'язково"` (skip keys that already exist).

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/functional/test_portal_detail_pages.py tests/functional/test_classroom_student_view.py tests/functional/test_quiz_and_teacher_scores.py tests/functional/test_squad_quiz.py -q` → PASS (update assertions on removed `data-scored-panel`).

- [ ] **Step 5: Commit** — `Show students a checklist of what counts the work`.

---

### Task 11: Breakdown readers, Classroom report, new-version hook

**Files:**
- Modify: `src/submissions_checker/services/gradebook.py:362-374,520-536`, `src/submissions_checker/api/routes/teacher_classroom.py:795-830`, `src/submissions_checker/services/google/ingest.py:391-415`, `src/submissions_checker/services/settle.py`
- Test: `tests/unit/test_gradebook*.py` (whichever tests `breakdown_line`; grep), `tests/functional/test_classroom_ingest.py`, `tests/functional/test_classroom_report.py`

**Interfaces:**
- Produces:
  - `gradebook.breakdown_line(breakdown, quiz_label="Тест") -> str | None` reads both the old `quiz_and_teacher_scores` shape and the new `items` shape: `"Тест 6/8 · Звіт 4/5 · Прийнято ✓"` (items with `max == 0` and type check render as `"<title> ✓"`; optional items not done are omitted).
  - review_score for `mode == "requirements"` = sum of `points` of score/check items.
  - `settle.reopen_after_new_version(db, subjects_assignment_id: int, student_id: int) -> None`: find the student's SA row; if its latest submission has `REJECTED_KEY`, drop the key (reassign dict) and transition happens via `settle()`; always call `settle()` (no-op for COMPLETED besides regrade).

- [ ] **Step 1: Failing tests**

```python
def test_breakdown_line_new_shape() -> None:
    b = {"mode": "requirements", "items": [
        {"key": "quiz", "type": "quiz", "title": "Тест", "state": "done", "points": 6, "max": 8},
        {"key": "report", "type": "score", "title": "Звіт", "state": "done", "points": 4, "max": 5},
        {"key": "star", "type": "score", "title": "Зірочка", "state": "pending", "points": 0, "max": 3},
        {"key": "accepted", "type": "check", "title": "Прийнято", "state": "done", "points": 0, "max": 0},
    ]}
    assert breakdown_line(b) == "Тест 6/8 · Звіт 4/5 · Прийнято ✓"
```

and in `tests/functional/test_classroom_ingest.py` a case: a rejected (FAILED, `rejected` in metadata) QUIZ_ONLY submission + new Classroom version ingested for the linked student → the submission leaves FAILED (`AWAITING_TEACHER_REVIEW` when the quiz is passed and scores pending) and `rejected` is gone.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** the readers as specified; in `teacher_classroom.py` replace `teacher_scores.criteria(grading_cfg)` with `settle.plan_for(assignment).of("score")` (fields `key`, `title`, `points` as max) and `llm_criteria(grading_cfg)` with `llm_criteria(assignment.config)`; in `ingest._ingest_submission` after `report.new_versions += 1` (before the commit — move the commit after it):

```python
    if link.student_id is not None and link.confirmed:
        await settle.reopen_after_new_version(db, assignment_id, link.student_id)
```

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/unit -q -k breakdown tests/functional/test_classroom_ingest.py tests/functional/test_classroom_report.py tests/functional/test_classroom_nightly.py tests/functional/test_gradebook*.py -q` → PASS.

- [ ] **Step 5: Commit** — `Read the requirements breakdown and reopen rejected work on a new version`.

---

### Task 12: Remove the mode machinery

**Files:**
- Modify: `src/submissions_checker/core/state_machine.py`, `src/submissions_checker/services/grading.py`, `src/submissions_checker/services/teacher_scores.py`, `src/submissions_checker/services/quiz_open.py`, `src/submissions_checker/db/models/subjects_assignment.py` (docstring), `docs/PLUGIN_AUTHORING.md`, `docs/feature_catalog.md`, `.claude/CLAUDE.md` (local file: core flows + `teacher_scores.py` line), `tests/unit/test_state_machine.py`, `tests/unit/test_grading.py`, `tests/unit/test_teacher_scores.py`, `tests/unit/test_requirements_translate.py`, `tests/e2e/steps/quiz_steps.py`

- [ ] **Step 1: Freeze the oracle.** In `tests/unit/test_requirements_translate.py` replace calls to `compute_grade` / `teacher_scores.compute` with literal expected grades: run the current tests once with a print of `old` per case (`-s`), paste the numbers into the parametrize tables as `expected`, and delete the oracle imports. Run → PASS.

- [ ] **Step 2: Delete** from `state_machine.py`: `test_passed_tests_only`, `test_passed_teacher`, `test_passed_quiz`, `ai_review_done_teacher`, `ai_review_done_completed`, `ai_review_passed_quiz`, `ai_review_skip_to_teacher`, `quiz_opened`, `quiz_passed`, `quiz_passed_teacher`, `quiz_failed`, `teacher_approve`, `teacher_send_quiz`, `dispute_regrade_passed`, `dispute_regrade_passed_teacher`. Keep: processing edges, `test_passed_ai`, `test_failed`, `ai_review_failed`, `retry_ai_review`, `start_ai_review`, `quiz_attempt_granted`, `teacher_reject`, `requeue_checks`, the settle events, `processing_done`. Then `grep -rn` each deleted name in `src/` and `tests/` — every hit must be gone (delete or rewrite the test to the settle event).

- [ ] **Step 3: Delete** `grading.compute_grade`, `GradeBreakdown`, `_blend`, `finalize_grade` (keep `squad_quiz_pct`, `_pct`); `teacher_scores.compute`, `quiz_points_for`, `parse_form`, `is_complete`, `ScoreError` (keep `MODE`, `Criterion`, `criteria`, `validate_assignment`, `is_scored_mode` — used by translation and legacy validation). Delete their unit tests. `grep -rn "finalize_grade\|compute_grade\|quiz_points_for\|teacher_scores.parse_form\|teacher_scores.is_complete" src tests` → no hits.

- [ ] **Step 4: Docs.** `docs/PLUGIN_AUTHORING.md`: replace the `review_mode` section with the `requirements`/`source` reference (table from the spec §1, validation rules, legacy note "`review_mode` is still read and translated; do not mix it with `requirements`"). `docs/feature_catalog.md`: state diagram → processing edges + settle events; board routes (`/scores/batch` added, approve removed, send-to-teacher removed). `.claude/CLAUDE.md`: core-flow bullet → "`_advance_after_tests` → `settle.settle` (requirements list; old modes translated by `requirements.from_config`)"; add `requirements.py`, `settle.py`, `board_scores.py` lines under services.

- [ ] **Step 5: Full verification**

Run: `uv run --frozen --extra dev pytest -q` → all pass.
Run: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/` → clean.
Run: `uv run --frozen mypy src/` → clean.
Run: `make e2e` → pass (fix `tests/e2e/steps/quiz_steps.py` approve steps to the board check).

- [ ] **Step 6: Commit** — `Remove review-mode transitions and grade blends` (body: settle is the only completion path now; old configs still work through translation).

---

### Task 13: GitHub link extraction (DOCX/PDF)

**Files:**
- Modify: `pyproject.toml` (add `"pypdf>=5.1.0"` to dependencies), `uv.lock` (`uv lock` — the one place `--frozen` is not used; commit the lock)
- Create: `src/submissions_checker/services/llm_grading/github_code.py`
- Test: `tests/unit/test_github_code_link.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class RepoRef: owner: str; repo: str; ref: str | None; path: str | None` with `url` property `https://github.com/{owner}/{repo}` (+ `/tree/{ref}/{path}`).
  - `def parse_repo_url(text: str) -> RepoRef | None` — first match in text.
  - `def find_repo_link(files: Iterable[tuple[str, bytes]]) -> RepoRef | None` — scans `.docx` (document.xml text + rels targets) then `.pdf` (page text + `/URI` annotations), in file order.

- [ ] **Step 1: Failing tests**

```python
"""Find the student's GitHub repository link in a DOCX or PDF report."""

from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import NameObject, TextStringObject, DictionaryObject

from submissions_checker.services.llm_grading import github_code as gh


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("див. https://github.com/ivan-p/lab4", ("ivan-p", "lab4", None, None)),
        ("(https://github.com/ivan/dist.git).", ("ivan", "dist", None, None)),
        ("https://github.com/ivan/dist/tree/main/lab4).", ("ivan", "dist", "main", "lab4")),
        ("https://github.com/ivan/dist/tree/dev", ("ivan", "dist", "dev", None)),
        ("http://www.github.com/a/b/", ("a", "b", None, None)),
        ("https://gist.github.com/a/b", None),
        ("no link here", None),
    ],
)
def test_parse_repo_url(text: str, expected: tuple | None) -> None:
    ref = gh.parse_repo_url(text)
    assert (None if ref is None else (ref.owner, ref.repo, ref.ref, ref.path)) == expected


def _docx(body_text: str, rel_target: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   f'<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body></w:document>')
        rels = "" if rel_target is None else (
            f'<Relationship Id="rId9" Type="hyperlink" Target="{rel_target}" TargetMode="External"/>')
        z.writestr("word/_rels/document.xml.rels", f"<Relationships>{rels}</Relationships>")
    return buf.getvalue()


def test_docx_text_link() -> None:
    ref = gh.find_repo_link([("Звіт.docx", _docx("Код: https://github.com/a/lab1"))])
    assert (ref.owner, ref.repo) == ("a", "lab1")


def test_docx_hyperlink_with_hidden_url() -> None:
    ref = gh.find_repo_link([("r.docx", _docx("мій репозиторій", "https://github.com/a/lab2"))])
    assert ref.repo == "lab2"


def _pdf_with_link(uri: str) -> bytes:
    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.add_annotation(page_number=0, annotation=Link(rect=(10, 10, 100, 30), url=uri))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_annotation_link() -> None:
    ref = gh.find_repo_link([("r.pdf", _pdf_with_link("https://github.com/b/lab3/tree/main/src"))])
    assert (ref.repo, ref.ref, ref.path) == ("lab3", "main", "src")


def test_broken_files_are_skipped() -> None:
    assert gh.find_repo_link([("r.pdf", b"not a pdf"), ("x.docx", b"nope")]) is None
```

(If `pypdf.annotations.Link(url=...)` differs in the installed version, build the `/Annots` entry with `DictionaryObject` — the unused imports above are for that fallback; drop whichever you do not use.)

- [ ] **Step 2: Run** `uv lock && uv run --frozen --extra dev pytest tests/unit/test_github_code_link.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** (top of `github_code.py`)

```python
"""The student's code from the GitHub link in their report, for the LLM grader.

Students keep code out of the report and link a public repository instead. We find the
first github.com link in the report (DOCX text and hyperlinks, PDF text and link
annotations), download the repository tarball into memory, keep the source files and
hand them to the judge. Nothing is written to disk or stored: only a manifest of what was
sent lands in the draft. Any failure degrades to grading the report alone.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:/tree/([^/\s)\]\"'<>,;]+)(?:/([^\s)\]\"'<>,;?#]+?))?)?/?(?=[\s)\]\"'<>,;.?#]|$)"
)


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str
    ref: str | None = None
    path: str | None = None

    @property
    def url(self) -> str:
        base = f"https://github.com/{self.owner}/{self.repo}"
        if self.ref:
            base += f"/tree/{self.ref}" + (f"/{self.path}" if self.path else "")
        return base


def parse_repo_url(text: str) -> RepoRef | None:
    m = _URL_RE.search(text)
    if m is None:
        return None
    owner, repo, ref, path = m.groups()
    return RepoRef(owner, repo, ref or None, (path or "").strip("/") or None)


def _docx_texts(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        out = []
        if "word/_rels/document.xml.rels" in names:
            out += re.findall(r'Target="([^"]+)"', z.read("word/_rels/document.xml.rels").decode("utf-8", "replace"))
        if "word/document.xml" in names:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            out.append("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)))
        return out


def _pdf_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out: list[str] = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            action = (annot.get_object().get("/A") or {})
            uri = action.get("/URI") if hasattr(action, "get") else None
            if uri:
                out.append(str(uri))
        out.append(page.extract_text() or "")
    return out


def find_repo_link(files: Iterable[tuple[str, bytes]]) -> RepoRef | None:
    """The first github.com repository link in the report files, or None."""
    for name, data in files:
        lower = name.lower()
        try:
            texts = _docx_texts(data) if lower.endswith(".docx") else (
                _pdf_texts(data) if lower.endswith(".pdf") else [])
        except Exception:  # noqa: BLE001 — a broken file just has no link
            logger.info("github_link_unreadable", file=name)
            continue
        for text in texts:
            ref = parse_repo_url(text)
            if ref is not None:
                return ref
    return None
```

Note: DOCX hyperlink targets come before body text so a hidden link wins over a typed one only within the same file — fine either way; the test pins only presence.

- [ ] **Step 4: Run** → PASS; run the pattern once against a sample of real filenames is not needed (no network).

- [ ] **Step 5: Commit** — `Find the student's GitHub link in the report` (body: students were told to link code instead of pasting it; pypdf added for PDF links).

---

### Task 14: Fetch and filter the repository in memory

**Files:**
- Modify: `src/submissions_checker/services/llm_grading/github_code.py`
- Test: `tests/unit/test_github_code_fetch.py`

**Interfaces:**
- Produces:
  - `MAX_DOWNLOAD = 20 * 1024 * 1024`, `MAX_FILES = 200`, `MAX_TOTAL = 1024 * 1024`, `KEEP_EXT = (".cpp", ".cc", ".c", ".h", ".hpp", ".rc", ".txt")`, `KEEP_NAMES = ("CMakeLists.txt",)`, `KEEP_SUFFIX = (".sln", ".vcxproj")`, `SKIP_DIRS = (".git", "x64", "Debug", "Release", ".vs", "build")`.
  - `@dataclass(frozen=True) class CodeFile: path: str; content: bytes`
  - `@dataclass(frozen=True) class CodeBundle: ref: RepoRef | None; commit: str | None; files: tuple[CodeFile, ...]; skipped: tuple[str, ...]; error: str | None` with `manifest() -> dict` = `{"url", "commit", "files": [{"path", "size"}], "skipped": [...], "error"}`.
  - `def select_files(tar_bytes: bytes, path: str | None) -> tuple[str | None, list[CodeFile], list[str]]` (commit = suffix of the top dir `<owner>-<repo>-<sha>`).
  - `async def fetch_code(http: httpx.AsyncClient, ref: RepoRef) -> CodeBundle` — never raises.

- [ ] **Step 1: Failing tests**

```python
"""fetch_code: tarball in memory, filtered, capped, failure-tolerant."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest

from submissions_checker.services.llm_grading import github_code as gh


def _tar(entries: dict[str, bytes], *, top: str = "a-lab-abc1234", link: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in entries.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"{top}/evil")
            info.type = tarfile.SYMTYPE
            info.linkname = link
            t.addfile(info)
    return buf.getvalue()


def test_select_files_filters_and_reports_commit() -> None:
    tar = _tar({"lab4/main.cpp": b"int main(){}", "lab4/x64/Debug/a.obj": b"x",
                "lab4/CMakeLists.txt": b"c", "README.md": b"r", ".git/config": b"g",
                "lab4/App.vcxproj": b"v"}, link="/etc/passwd")
    commit, files, skipped = gh.select_files(tar, None)
    assert commit == "abc1234"
    assert sorted(f.path for f in files) == ["lab4/App.vcxproj", "lab4/CMakeLists.txt", "lab4/main.cpp"]
    assert "evil" not in [f.path for f in files]


def test_select_files_under_path() -> None:
    tar = _tar({"lab4/main.cpp": b"a", "lab3/main.cpp": b"b"})
    _, files, _ = gh.select_files(tar, "lab4")
    assert [f.path for f in files] == ["lab4/main.cpp"]


def test_caps_skip_the_rest() -> None:
    tar = _tar({f"f{i:03}.cpp": b"x" * 10 for i in range(gh.MAX_FILES + 5)})
    _, files, skipped = gh.select_files(tar, None)
    assert len(files) == gh.MAX_FILES and len(skipped) == 5
    tar = _tar({"big.cpp": b"x" * (gh.MAX_TOTAL + 1), "small.cpp": b"y"})
    _, files, skipped = gh.select_files(tar, None)
    assert [f.path for f in files] == ["small.cpp"] and skipped == ["big.cpp"]


def test_unsafe_paths_dropped() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name in ("a-b-c/../../x.cpp", "/abs.cpp"):
            info = tarfile.TarInfo(name)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    _, files, _ = gh.select_files(buf.getvalue(), None)
    assert files == []


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_ok_and_url() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=_tar({"main.cpp": b"int"}))

    ref = gh.RepoRef("a", "lab", "main", None)
    async with _client(handler) as http:
        bundle = await gh.fetch_code(http, ref)
    assert seen == ["https://api.github.com/repos/a/lab/tarball/main"]
    assert bundle.error is None and [f.path for f in bundle.files] == ["main.cpp"]
    assert bundle.manifest()["files"] == [{"path": "main.cpp", "size": 3}]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "error"), [(404, "not_found"), (403, "forbidden_or_rate_limited")])
async def test_fetch_http_errors_degrade(status: int, error: str) -> None:
    async with _client(lambda r: httpx.Response(status)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.files == () and bundle.error == error


@pytest.mark.asyncio
async def test_fetch_oversized_aborts() -> None:
    big = b"x" * (gh.MAX_DOWNLOAD + 1)
    async with _client(lambda r: httpx.Response(200, content=big)) as http:
        bundle = await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert bundle.error == "too_large"


@pytest.mark.asyncio
async def test_nothing_written_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    async with _client(lambda r: httpx.Response(200, content=_tar({"m.cpp": b"1"}))) as http:
        await gh.fetch_code(http, gh.RepoRef("a", "b"))
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run** → FAIL (`no attribute 'select_files'`).

- [ ] **Step 3: Implement** (append; add `import tarfile`, `import posixpath`, `import httpx`)

```python
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_FILES = 200
MAX_TOTAL = 1024 * 1024
TIMEOUT = 20.0
KEEP_EXT = (".cpp", ".cc", ".c", ".h", ".hpp", ".rc", ".txt")
KEEP_SUFFIX = (".sln", ".vcxproj")
SKIP_DIRS = frozenset({".git", "x64", "Debug", "Release", ".vs", "build"})


@dataclass(frozen=True)
class CodeFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class CodeBundle:
    ref: RepoRef | None
    commit: str | None = None
    files: tuple[CodeFile, ...] = ()
    skipped: tuple[str, ...] = ()
    error: str | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "url": self.ref.url if self.ref else None,
            "commit": self.commit,
            "files": [{"path": f.path, "size": len(f.content)} for f in self.files],
            "skipped": list(self.skipped),
            "error": self.error,
        }


def _keep(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    return name.endswith(KEEP_EXT) or name.endswith(KEEP_SUFFIX)


def select_files(tar_bytes: bytes, path: str | None) -> tuple[str | None, list[CodeFile], list[str]]:
    """Source files from a GitHub tarball, in memory; unsafe or filtered entries dropped."""
    files: list[CodeFile] = []
    skipped: list[str] = []
    commit: str | None = None
    total = 0
    prefix = (path.strip("/") + "/") if path else ""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for member in tar:
            name = member.name
            if name.startswith("/") or ".." in name.split("/") or not member.isfile():
                continue
            top, _, rel = name.partition("/")
            if commit is None and "-" in top:
                commit = top.rsplit("-", 1)[1]
            rel = posixpath.normpath(rel) if rel else ""
            if not rel or rel.startswith("..") or not rel.startswith(prefix) or not _keep(rel):
                continue
            if len(files) >= MAX_FILES or total + member.size > MAX_TOTAL:
                skipped.append(rel)
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            data = handle.read()
            files.append(CodeFile(rel, data))
            total += len(data)
    return commit, files, skipped


async def fetch_code(http: httpx.AsyncClient, ref: RepoRef) -> CodeBundle:
    """The repository's source files, or a bundle naming why there are none. Never raises."""
    url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/tarball" + (
        f"/{ref.ref}" if ref.ref else "")
    try:
        async with http.stream("GET", url, follow_redirects=True, timeout=TIMEOUT,
                               headers={"Accept": "application/vnd.github+json"}) as resp:
            if resp.status_code == 404:
                return CodeBundle(ref, error="not_found")
            if resp.status_code in (401, 403, 429):
                return CodeBundle(ref, error="forbidden_or_rate_limited")
            if resp.status_code != 200:
                return CodeBundle(ref, error=f"http_{resp.status_code}")
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_DOWNLOAD:
                    return CodeBundle(ref, error="too_large")
        commit, files, skipped = select_files(bytes(buf), ref.path)
        del buf
    except (httpx.HTTPError, tarfile.TarError, OSError) as exc:
        logger.info("github_fetch_failed", url=ref.url, error=type(exc).__name__)
        return CodeBundle(ref, error=type(exc).__name__)
    return CodeBundle(ref, commit, tuple(files), tuple(skipped),
                      None if files else "no_source_files")
```

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/unit/test_github_code_fetch.py tests/unit/test_github_code_link.py -q` → PASS.

- [ ] **Step 5: Commit** — `Fetch the linked repository in memory with caps` (body: unauthenticated tarball, filtered to sources, nothing touches disk so the VPS stays clean).

---

### Task 15: Send the code to the judge and record what was sent

**Files:**
- Modify: `src/submissions_checker/services/llm_grading/judge.py` (`GradingRequest.code_source: dict | None = None`), `prompt.py` (`build_prompt` CODE section), `runner.py` (`build_request`, `grade_one`), `templates/teacher_llm_report.html`, `api/routes/teacher_classroom.py` (pass `code_source`), `i18n/uk.yml` (`classroom.code_source_*`)
- Test: `tests/unit/test_llm_prompt.py`, `tests/functional/test_classroom_nightly.py` (or the file that tests `grade_one`; grep `build_request`)

**Interfaces:**
- Consumes: `github_code.find_repo_link`, `fetch_code`, `CodeBundle` (Tasks 13–14).
- Produces:
  - `CodeFetcher = Callable[[RepoRef], Awaitable[CodeBundle]]`
  - `build_request(db, grading, storage, *, fetch: CodeFetcher | None = None) -> GradingRequest` — after loading report files: `ref = find_repo_link((f.name, f.content) for f in files)`; `bundle = await fetch(ref) if ref and fetch else CodeBundle(ref, error="no_link" if ref is None else "disabled")`; code files appended as `WorkFile(f"github/{c.path}", c.content, "text/plain")`; `code_source=bundle.manifest()`.
  - `grade_one(..., fetch: CodeFetcher | None = None)` stores `grading.draft["code_source"] = req.code_source`.
  - `grading_loop(..., fetch: CodeFetcher | None = None)` passes it through; the scheduler job that calls `grading_loop` creates `httpx.AsyncClient()` once per run and passes `fetch=lambda ref: github_code.fetch_code(http, ref)` (grep the caller of `grading_loop`).
  - Prompt CODE section (only when `code_source` given):

```
=== CODE FROM THE STUDENT'S GITHUB (data) ===
repository: <url>   commit: <commit or "unknown">
files: <n> sent, <m> skipped
error: <error>        (only when set)
Judge whether the implementation matches the task from these files (named github/...). If no code is attached, say so in the comment and grade the implementation criterion from the report only.
```

- [ ] **Step 1: Failing tests**

```python
def test_prompt_has_code_section() -> None:
    req = GradingRequest(task="t", instructions="", criteria=[JudgeCriterion("r", "R", 5, "x")],
                         files=[WorkFile("github/main.cpp", b"int", "text/plain")],
                         code_source={"url": "https://github.com/a/b", "commit": "abc", "files":
                                      [{"path": "main.cpp", "size": 3}], "skipped": [], "error": None})
    text = build_prompt(req)
    assert "=== CODE FROM THE STUDENT'S GITHUB (data) ===" in text and "commit: abc" in text


def test_prompt_without_code_source_unchanged() -> None:
    req = GradingRequest(task="t", instructions="", criteria=[], files=[])
    assert "GITHUB" not in build_prompt(req)
```

and a functional case next to the existing `grade_one` tests: a work whose DOCX contains `https://github.com/a/lab`; fake judge records the request; `fetch` returns a bundle with one file → the judge saw `github/main.cpp` and `grading.draft["code_source"]["commit"] == "abc"`; a second case where `fetch` returns `error="not_found"` → grading is DONE and `code_source.error == "not_found"`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** as in Interfaces (`build_prompt` appends the section before the student-work manifest; `_one_line` the URL and error). In `teacher_llm_report.html`, under the criteria table, render `grading.draft.code_source`: repo link, commit, file count, skipped count, error via `vocab.classroom.code_source_error_<code>` with a fallback to the raw code.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/unit/test_llm_prompt.py tests/unit/test_llm_judge_sidecar.py tests/functional/test_classroom_nightly.py tests/functional/test_classroom_report.py -q` → PASS.

- [ ] **Step 5: Commit** — `Give the LLM grader the student's GitHub code`.

---

### Task 16: Backfill command

**Files:**
- Create: `src/submissions_checker/cli/backfill_requirements.py`
- Test: `tests/functional/test_backfill_requirements.py`

**Interfaces:**
- Consumes: `settle.settle`, `settle.plan_for`, `squads.scope_sa_ids`.
- Produces: `async def backfill(db, subject_id: int, *, apply: bool) -> Report` where `Report` has `ticked: int`, `settled: int`, `status_changes: list[tuple[int, str, str]]`, `grade_changes: list[tuple[int, int | None, int | None]]`; CLI `python -m submissions_checker.cli.backfill_requirements --subject 1 [--apply]` prints the report; exits 1 (and rolls back) when `grade_changes` is non-empty; without `--apply` always rolls back.

Logic: for every assignment of the subject with check items, for every SA row whose latest scope submission is `COMPLETED`, set every check key to 1 on every scope row (`teacher_scores = {**old, key: 1}`); record grades of all SA rows of the subject; `settle` every SA row that has a submission; compare grades.

- [ ] **Step 1: Failing test**

```python
async def test_backfill_ticks_completed_and_keeps_grades(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                             status=SubmissionStatus.COMPLETED,
                                             scores={"report": 4})
    sa.grade = 10
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    report = await backfill(db, asg.subject_id, apply=True)
    await db.refresh(sa)
    assert sa.teacher_scores == {"report": 4, "accepted": 1}
    assert report.ticked == 1 and report.grade_changes == [] and sa.grade == 10


async def test_backfill_aborts_on_grade_change(db, teacher, make_student):
    _, asg, student, sa, sub = await arrange(db, teacher, make_student, config=CFG,
                                             status=SubmissionStatus.COMPLETED, scores={"report": 4})
    sa.grade = 3  # inconsistent with the facts
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    await db.commit()
    report = await backfill(db, asg.subject_id, apply=True)
    assert report.grade_changes == [(sa.id, 3, 10)]
    await db.refresh(sa)
    assert sa.grade == 3 and "accepted" not in (sa.teacher_scores or {})
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with `async with db.begin_nested()` around the whole run and `raise _Abort` to roll back when `grade_changes` or not `apply`; `main()` builds a session via `get_session_factory()` like the other CLIs (see `cli/migrate_uploads.py`).

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `Add the requirements backfill with a no-grade-change guard`.

---

### Task 17: Subject repo config v8

**Files (repo `/home/vampir/petProjects/basicsOfTheDistributedSoftwareDevelopment`, branch `master`):**
- Modify: `config.yml` (labs 1–7), lab description files referenced by `description`/`task_file`

- [ ] **Step 1:** For each of lab1…lab7 replace `review_mode` + `grading` with:

```yaml
    source: classroom
    requirements:
      - {type: quiz, key: quiz, points: 8}
      - {type: score, key: report, title: "Звіт", max: 5, llm: *llm_report_labN}
      - {type: score, key: star, title: "Зірочка", max: 3, optional: true, llm: *llm_star_labN}  # lab3+ only, keep as it is today
      - {type: check, key: accepted, title: "Прийнято викладачем", points: 0}
```

keeping each lab's existing `max_grade`, rubric anchors (today's `teacher_criteria[].requirements` text moves to `llm:`; a criterion with `llm: false` gets no `llm`), `quiz:` and `llm_grading:` blocks.

- [ ] **Step 2:** In `_llm_instructions` add: "Код програми студенти не вставляють у звіт: він у публічному GitHub-репозиторії, посилання на який є у звіті. Файли коду додано окремо з іменами github/…; оцінюй відповідність реалізації варіанту за ними. Якщо коду немає, вкажи це в коментарі." In each report rubric, requirement 3 becomes "реалізація відповідає варіанту — перевіряється за кодом із GitHub-репозиторію, посилання на який наведено у звіті". In each lab description add: "Не вставляйте код у звіт. Викладіть його в публічний репозиторій на GitHub і дайте посилання у звіті."

- [ ] **Step 3: Validate locally** against the new backend: `uv run --frozen runner …` has no config-apply path, so apply the built zip on the local dev stack (`make up`, teacher UI → apply config) and confirm "applied"; or run `ConfigApplyService` validation in a one-off: `uv run --frozen python -c "…"` loading `config.yml` and calling `ConfigApplyService()._validate_requirements(cfg)`.

- [ ] **Step 4: Commit + tag** — commit "Declare lab requirements and take code from GitHub", tag `v8`, push branch and tag (CI builds the release zip).

---

### Task 18: Rollout (ops, no code)

Backup before every step: `scripts/ops/run-prod-backup.sh --now` and wait for it to finish (`--status`).

- [ ] **Step 1:** Open the PR from `grading-components-2026-10`, merge to main after review; wait for Watchtower; confirm `/version` shows the merge commit.
- [ ] **Step 2: Read-only check of v7 translation on prod.** Pipe a script into app-1 that, for every SA row of subject 1 with a submission, runs `gather_facts` + `evaluate` (no `settle`, no writes) and prints rows whose computed grade ≠ stored grade (expected: none for completed rows; incomplete rows have no grade). Stop if any differ.
- [ ] **Step 3:** Apply config v8 (pipe the v8 zip into app-1 and run `apply_cfg.py` from the scratchpad, as for v7).
- [ ] **Step 4:** `python -m submissions_checker.cli.backfill_requirements --subject 1` (dry run) → review; then `--apply`. It must report `grade_changes: []`.
- [ ] **Step 5:** Spot-check the lab4 board: squads 1 and 4 show the quiz ✓, report from the draft as placeholder, an unticked «Прийнято викладачем», no grade. Check one completed lab1 row shows «✓ Прийнято» and its old grade.

---

## Self-Review notes (done while writing)

- Spec coverage: §1 config → Tasks 1, 2, 8; §2 evaluation/settle → 3, 5, 6, 7, 12; §3 UI → 9, 10, 11; §4 GitHub → 13, 14, 15; §5 migration/rollout → 16, 17, 18; testing list → spread across tasks.
- Deviations from the spec, decided here: the saved check renders «✓ <title>» without a date (no schema change, no date stored); the AI-flagged reroute of `tests_then_ai_then_quiz` is dropped (flag badge stays, teacher can Reject); "Затвердити routes gone" means `action=approve` answers 400 (there is no separate approve URL); `send-to-teacher` is removed.
- Types: `settle.plan_for(asg)` everywhere (sync function); `requirements.from_config(cfg, span=…)`; `llm_criteria(assignment_config)` (signature change, Task 8).
