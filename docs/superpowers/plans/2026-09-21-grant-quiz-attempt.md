# Grant Extra Quiz Attempt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A teacher button on the assignment board that gives one exhausted student one more quiz attempt, through an explicit state-machine event, without deleting attempt history.

**Architecture:** A per-student counter in `submissions.source_metadata["quiz_extra_attempts"]` raises the effective attempt cap; a new `quiz_attempt_granted` event moves `FAILED → QUIZ_SENT` (and `QUIZ_SENT → QUIZ_SENT` for a second exhausted squad member). A small service `services/quiz_grants.py` owns the precondition and the grant; a teacher route audits, notifies in-app and redirects to the board; the three attempt-cap consumers read the effective cap.

**Tech Stack:** FastAPI, SQLAlchemy async (Postgres, JSONB), Jinja, pytest + testcontainers (functional layer drives the real app).

**Spec:** `docs/superpowers/specs/2026-09-21-grant-quiz-attempt-design.md`

## Global Constraints

- Always `uv run --frozen`; never let `uv.lock` change.
- Never assign `submission.status` directly; use `transition()`.
- Never delete or edit `quiz_attempts` rows.
- JSONB writes reassign the dict (`submission.source_metadata = new_dict`), never mutate in place.
- UI strings in Ukrainian via `i18n/uk.yml` and `vocab.*`.
- Metadata key is exactly `quiz_extra_attempts`; JSON keys are strings (`str(student_id)`).
- Event name is exactly `quiz_attempt_granted`; audit action is exactly `grant_quiz_attempt`.
- Commit messages: imperative subject ≤72 chars, body explains why, trailer `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Before claiming a task done: run the named test command and read its output.

---

### Task 1: State-machine event

**Files:**
- Modify: `src/submissions_checker/core/state_machine.py:52-56` (the `FAILED` block) and the `QUIZ_SENT` block at lines 40-44
- Test: `tests/unit/test_state_machine.py`

**Interfaces:**
- Produces: `transition(submission, "quiz_attempt_granted")` valid from `FAILED` (→ `QUIZ_SENT`) and `QUIZ_SENT` (→ `QUIZ_SENT`).

- [ ] **Step 1: Add the edges to the LEGAL / ILLEGAL tables**

In `tests/unit/test_state_machine.py`, inside `LEGAL` after the `dispute_regrade_passed_teacher` line add:

```python
    # A teacher grants one more quiz attempt to an exhausted student. The self-loop on
    # QUIZ_SENT is for a squad whose second member exhausted after the first was granted.
    (S.FAILED, "quiz_attempt_granted", S.QUIZ_SENT),
    (S.QUIZ_SENT, "quiz_attempt_granted", S.QUIZ_SENT),
```

Inside `ILLEGAL` after `(S.AWAITING_TEACHER_REVIEW, "dispute_regrade_passed"),` add:

```python
    # A grant only re-opens a quiz; it never un-completes or skips a teacher review.
    (S.COMPLETED, "quiz_attempt_granted"),
    (S.AWAITING_TEACHER_REVIEW, "quiz_attempt_granted"),
    (S.TEST_FAILED, "quiz_attempt_granted"),
```

- [ ] **Step 2: Run to verify the two legal cases fail**

Run: `uv run --frozen --extra dev pytest tests/unit/test_state_machine.py -q -k quiz_attempt_granted`
Expected: 2 FAILED (legal ones raise `InvalidTransitionError`), 3 passed.

- [ ] **Step 3: Add the edges**

In `src/submissions_checker/core/state_machine.py` change the `QUIZ_SENT` block to:

```python
    SubmissionStatus.QUIZ_SENT: {
        "quiz_passed": SubmissionStatus.COMPLETED,
        "quiz_passed_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
        "quiz_failed": SubmissionStatus.FAILED,
        # Squads only: member A exhausted (→ FAILED), was granted an attempt (→ QUIZ_SENT),
        # and member B — mid-attempt at the time — has since exhausted too. B's grant
        # lands while the submission is already QUIZ_SENT; the self-loop keeps one path.
        "quiz_attempt_granted": SubmissionStatus.QUIZ_SENT,
    },
```

and the `FAILED` block to:

```python
    SubmissionStatus.FAILED: {
        "dispute_regrade_passed": SubmissionStatus.COMPLETED,
        "dispute_regrade_passed_teacher": SubmissionStatus.AWAITING_TEACHER_REVIEW,
        # A teacher grants one more quiz attempt to a student who exhausted
        # max_quiz_attempts (services.quiz_grants checks that this is why it failed).
        "quiz_attempt_granted": SubmissionStatus.QUIZ_SENT,
    },
```

Also update the comment above `FAILED` — replace "The single exception is a teacher accepting a broken-question dispute" with "The exceptions are a teacher accepting a broken-question dispute, and a teacher granting an extra quiz attempt".

- [ ] **Step 4: Run the whole state-machine file**

Run: `uv run --frozen --extra dev pytest tests/unit/test_state_machine.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/submissions_checker/core/state_machine.py tests/unit/test_state_machine.py
git commit -m "Add quiz_attempt_granted transition out of FAILED

A student who exhausts max_quiz_attempts is stuck in terminal FAILED and
the only way back today is hand-editing submissions.status. Naming the
edge keeps the 'never assign status directly' invariant and pins where
a grant may land: back to QUIZ_SENT only, never past a teacher review.
The QUIZ_SENT self-loop exists for a squad whose second member exhausted
after the first was already granted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `quiz_grants` pure helpers

**Files:**
- Create: `src/submissions_checker/services/quiz_grants.py`
- Test: `tests/unit/test_quiz_grants.py`

**Interfaces:**
- Produces:
  - `EXTRA_KEY = "quiz_extra_attempts"`
  - `extra_attempts(submission, student_id: int) -> int`
  - `effective_max_attempts(base: int | None, submission, student_id: int) -> int | None`
  - `class GrantError(Exception)`
  - `is_exhausted(attempts: Sequence[QuizAttempt], effective_max: int | None) -> bool`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_quiz_grants.py`:

```python
"""Pure helpers of services.quiz_grants — no DB."""

from __future__ import annotations

from types import SimpleNamespace

from submissions_checker.db.models.enums import QuizAttemptStatus
from submissions_checker.services.quiz_grants import (
    EXTRA_KEY,
    effective_max_attempts,
    extra_attempts,
    is_exhausted,
)


def _sub(meta):
    return SimpleNamespace(source_metadata=meta)


def _attempt(status=QuizAttemptStatus.COMPLETED, is_passed=False):
    return SimpleNamespace(status=status, is_passed=is_passed)


def test_extra_attempts_defaults_to_zero() -> None:
    assert extra_attempts(_sub({}), 7) == 0
    assert extra_attempts(_sub(None), 7) == 0
    assert extra_attempts(_sub({EXTRA_KEY: {}}), 7) == 0


def test_extra_attempts_reads_string_key() -> None:
    # JSON object keys are strings; the student id is an int in Python.
    assert extra_attempts(_sub({EXTRA_KEY: {"7": 2}}), 7) == 2
    assert extra_attempts(_sub({EXTRA_KEY: {"7": 2}}), 8) == 0


def test_effective_max_adds_extra_and_keeps_none() -> None:
    sub = _sub({EXTRA_KEY: {"7": 1}})
    assert effective_max_attempts(3, sub, 7) == 4
    assert effective_max_attempts(3, sub, 8) == 3
    assert effective_max_attempts(None, sub, 7) is None


def test_is_exhausted_requires_cap_and_only_failed_terminal_attempts() -> None:
    assert not is_exhausted([], 1)  # nothing used yet
    assert not is_exhausted([_attempt()], None)  # no cap → never exhausted
    assert is_exhausted([_attempt()], 1)
    assert not is_exhausted([_attempt()], 2)
    assert not is_exhausted([_attempt(is_passed=True)], 1)  # passed: nothing to grant
    assert not is_exhausted([_attempt(status=QuizAttemptStatus.IN_PROGRESS)], 1)
    assert is_exhausted([_attempt(status=QuizAttemptStatus.VIOLATION_FAIL)], 1)
    assert is_exhausted([_attempt(status=QuizAttemptStatus.TIMED_OUT), _attempt()], 2)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --frozen --extra dev pytest tests/unit/test_quiz_grants.py -q`
Expected: ImportError / ModuleNotFoundError for `services.quiz_grants`.

- [ ] **Step 3: Create the module with the pure helpers**

Create `src/submissions_checker/services/quiz_grants.py`:

```python
"""Teacher-granted extra quiz attempts.

A student who uses up ``max_quiz_attempts`` without passing sends the submission to
``FAILED``. A teacher may hand that one student one more attempt. The grant is recorded per
student in ``submissions.source_metadata["quiz_extra_attempts"]`` (so a fresh upload starts
clean and squad members are counted apart), raises the effective attempt cap everywhere the
cap is read, and moves the submission back to ``QUIZ_SENT`` through an explicit event.
Nothing here touches ``quiz_attempts`` rows — history stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from submissions_checker.db.models.enums import QuizAttemptStatus

EXTRA_KEY = "quiz_extra_attempts"

_TERMINAL = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)


class GrantError(Exception):
    """The (submission, student) pair is not stuck on an exhausted quiz."""


def extra_attempts(submission: Any, student_id: int) -> int:
    """Extra attempts granted to this student on this submission (0 when none)."""
    meta = submission.source_metadata or {}
    extras = meta.get(EXTRA_KEY) or {}
    return int(extras.get(str(student_id), 0))


def effective_max_attempts(base: int | None, submission: Any, student_id: int) -> int | None:
    """The config's cap plus every grant; ``None`` stays ``None`` (no cap at all)."""
    if base is None:
        return None
    return int(base) + extra_attempts(submission, student_id)


def is_exhausted(attempts: Sequence[Any], effective_max: int | None) -> bool:
    """True when the student has used every attempt they are allowed and none passed.

    Needs at least one attempt, every attempt terminal (nothing still running), no pass,
    and a cap to exhaust — an uncapped quiz can never be "used up".
    """
    if effective_max is None or not attempts:
        return False
    if any(a.status not in _TERMINAL for a in attempts):
        return False
    if any(a.is_passed for a in attempts):
        return False
    return len(attempts) >= effective_max
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --frozen --extra dev pytest tests/unit/test_quiz_grants.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/submissions_checker/services/quiz_grants.py tests/unit/test_quiz_grants.py
git commit -m "Add quiz_grants helpers for the extra-attempt allowance

The allowance lives in submissions.source_metadata keyed by student id
so it is scoped to one upload (a re-upload starts clean) and counted per
squad member. Keeping the cap arithmetic in one place means the quiz
gate, the finalizer and the student page cannot drift apart.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `grantable_students` and `grant_extra_attempt` (DB)

**Files:**
- Modify: `src/submissions_checker/services/quiz_grants.py`
- Test: `tests/functional/test_teacher_grant_quiz_attempt.py` (new; uses the functional `db` fixture, no HTTP yet)

**Interfaces:**
- Consumes: `transition` from Task 1; helpers from Task 2; `squads.squad_for_submission(db, submission)`.
- Produces:
  - `async def grantable_students(db, submission) -> list[int]`
  - `async def grant_extra_attempt(db, submission, student_id: int) -> int` (new extra total; raises `GrantError`; no commit)
  - `async def base_max_attempts(db, submission) -> int | None`

- [ ] **Step 1: Write the failing service tests**

Create `tests/functional/test_teacher_grant_quiz_attempt.py`:

```python
"""Teacher grants one more quiz attempt to a student who exhausted max_quiz_attempts.

Service-level tests first (real Postgres, no HTTP), then the route and the board.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    AuditLog,
    QuizAttempt,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import (
    QuizAttemptStatus,
    SubmissionSourceType,
    SubmissionStatus,
    UserRole,
)
from submissions_checker.db.models.notification import Notification
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import quiz_grants
from submissions_checker.services.quiz_grants import GrantError
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio

QUIZ = {
    "questions": [
        {"type": "single_choice", "text": "q", "points": 1, "options": ["w", "r"], "correct": 1}
    ],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.6,
    "max_quiz_attempts": 2,
}


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _arrange(db, teacher, make_student, *, quiz=QUIZ, status=SubmissionStatus.FAILED):
    """Owned subject, one quiz assignment pinned to a config, one enrolled student with a
    submission in ``status``. Returns (subject, assignment, student, sa, submission)."""
    subject = Subject(name="Grantland", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"h{subject.id}",
        config={"assignments": {"l1": {"review_mode": "tests_then_quiz", "quiz": quiz}}},
    )
    asg = SubjectsAssignment(
        subject_id=subject.id, title="L1", code="l1", max_grade=100,
        config={"review_mode": "tests_then_quiz"},
    )
    db.add_all([cfg, asg])
    await db.commit()
    await db.refresh(cfg)
    await db.refresh(asg)
    student = await make_student(full_name="Olha O")
    student.recording_consent_at = datetime.now(UTC)
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    sa = StudentAssignment(student_id=student.id, subjects_assignment_id=asg.id)
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=status,
        plugin_config_id=cfg.id,
        test_results={"tests": []},
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return subject, asg, student, sa, sub


async def _attempt(
    db, sub, student_id, *, status=QuizAttemptStatus.COMPLETED, is_passed=False
) -> QuizAttempt:
    a = QuizAttempt(
        submission_id=sub.id,
        student_id=student_id,
        plugin_config_id=sub.plugin_config_id,
        plugin_config_version=1,
        questions_snapshot=[
            {
                "id": 0,
                "type": "SINGLE_CHOICE",
                "text": "q",
                "points": 1,
                "is_required": False,
                "config": {"options": ["w", "r"], "correct": 1},
            }
        ],
        config_snapshot={"pass_threshold_pct": 0.6, "max_quiz_attempts": 2},
        started_at=datetime.now(UTC),
        submitted_at=datetime.now(UTC) if status != QuizAttemptStatus.IN_PROGRESS else None,
        status=status,
        is_passed=is_passed,
        score=1 if is_passed else 0,
        max_score=1,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _exhausted(db, teacher, make_student):
    subject, asg, student, sa, sub = await _arrange(db, teacher, make_student)
    await _attempt(db, sub, student.id)
    await _attempt(db, sub, student.id, status=QuizAttemptStatus.TIMED_OUT)
    return subject, asg, student, sa, sub


# ── Service ──────────────────────────────────────────────────────────────────


async def test_grantable_when_failed_and_exhausted(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    assert await quiz_grants.grantable_students(db, sub) == [student.id]


async def test_not_grantable_when_not_exhausted(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _arrange(db, teacher, make_student)
    await _attempt(db, sub, student.id)  # 1 of 2 used
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_not_grantable_when_an_attempt_passed(db, teacher, make_student) -> None:
    # quiz_then_teacher + teacher reject: FAILED, attempts exist, but one passed.
    _s, _a, student, _sa, sub = await _arrange(db, teacher, make_student)
    await _attempt(db, sub, student.id)
    await _attempt(db, sub, student.id, is_passed=True)
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_not_grantable_when_failed_by_teacher_reject(db, teacher, make_student) -> None:
    # tests_then_teacher reject: FAILED with no attempts at all.
    _s, _a, _st, _sa, sub = await _arrange(db, teacher, make_student)
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_not_grantable_without_a_cap(db, teacher, make_student) -> None:
    uncapped = {k: v for k, v in QUIZ.items() if k != "max_quiz_attempts"}
    _s, _a, student, _sa, sub = await _arrange(db, teacher, make_student, quiz=uncapped)
    await _attempt(db, sub, student.id)
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_not_grantable_from_completed(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _arrange(
        db, teacher, make_student, status=SubmissionStatus.COMPLETED
    )
    await _attempt(db, sub, student.id)
    await _attempt(db, sub, student.id)
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_grant_bumps_allowance_and_reopens_quiz(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    before = (await db.execute(select(QuizAttempt.id).order_by(QuizAttempt.id))).scalars().all()

    assert await quiz_grants.grant_extra_attempt(db, sub, student.id) == 1
    await db.commit()
    await db.refresh(sub)

    assert sub.status == SubmissionStatus.QUIZ_SENT
    assert sub.source_metadata["quiz_extra_attempts"] == {str(student.id): 1}
    after = (await db.execute(select(QuizAttempt.id).order_by(QuizAttempt.id))).scalars().all()
    assert after == before  # history untouched
    # Now the student has 3 allowed, 2 used: no longer grantable until they use it.
    assert await quiz_grants.grantable_students(db, sub) == []


async def test_grant_refuses_wrong_student_and_not_exhausted(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    with pytest.raises(GrantError):
        await quiz_grants.grant_extra_attempt(db, sub, student.id + 1000)
    await quiz_grants.grant_extra_attempt(db, sub, student.id)
    with pytest.raises(GrantError):
        await quiz_grants.grant_extra_attempt(db, sub, student.id)


async def test_second_grant_after_refail_counts_to_two(db, teacher, make_student) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    await quiz_grants.grant_extra_attempt(db, sub, student.id)
    await db.commit()
    await _attempt(db, sub, student.id)  # used the granted one, failed again
    # Finalizer would move it to FAILED (Task 5); emulate through the state machine.
    from submissions_checker.core.state_machine import transition

    transition(sub, "quiz_failed")
    await db.commit()
    assert await quiz_grants.grant_extra_attempt(db, sub, student.id) == 2
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q`
Expected: AttributeError `grantable_students` / `grant_extra_attempt` not defined (needs Docker for the Postgres testcontainer).

- [ ] **Step 3: Add the DB-backed functions**

Append to `src/submissions_checker/services/quiz_grants.py` (and extend the imports at the top):

```python
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import QuizAttemptStatus, SubmissionStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads

_GRANTABLE_STATUSES = (SubmissionStatus.FAILED, SubmissionStatus.QUIZ_SENT)


async def base_max_attempts(db: AsyncSession, submission: Submission) -> int | None:
    """``max_quiz_attempts`` from the config pinned to this submission, or None."""
    if submission.plugin_config_id is None:
        return None
    code = await db.scalar(
        select(SubjectsAssignment.code)
        .join(StudentAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id)
        .where(StudentAssignment.id == submission.students_assignment_id)
    )
    cfg = await db.get(SubjectPluginConfig, submission.plugin_config_id)
    if cfg is None or not code:
        return None
    quiz = cfg.config.get("assignments", {}).get(code, {}).get("quiz") or {}
    value = quiz.get("max_quiz_attempts")
    return int(value) if value is not None else None


async def _students_on(db: AsyncSession, submission: Submission) -> list[int]:
    """Solo: the owner. Squad: every currently enrolled member (unenrolled ones no longer
    block completion, so they cannot be granted anything either)."""
    squad = await squads.squad_for_submission(db, submission)
    if squad is None:
        owner = await db.scalar(
            select(StudentAssignment.student_id).where(
                StudentAssignment.id == submission.students_assignment_id
            )
        )
        return [owner] if owner is not None else []
    member_ids = {m.student_id for m in squad.members}
    rows = await db.execute(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == squad.subject_id,
            SubjectsStudents.student_id.in_(member_ids),
        )
    )
    return sorted(sid for (sid,) in rows)


async def _attempts_of(
    db: AsyncSession, submission: Submission, student_id: int
) -> list[QuizAttempt]:
    # Same filter as the student quiz gate: this student's rows plus legacy NULL rows,
    # which predate per-student ids and belong to a solo submission by this student.
    rows = await db.execute(
        select(QuizAttempt).where(
            QuizAttempt.submission_id == submission.id,
            or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
        )
    )
    return list(rows.scalars().all())


async def _is_grantable(
    db: AsyncSession, submission: Submission, student_id: int, base: int | None
) -> bool:
    if submission.status not in _GRANTABLE_STATUSES or base is None:
        return False
    attempts = await _attempts_of(db, submission, student_id)
    return is_exhausted(attempts, effective_max_attempts(base, submission, student_id))


async def grantable_students(db: AsyncSession, submission: Submission) -> list[int]:
    """Students on this submission who are stuck on an exhausted quiz."""
    base = await base_max_attempts(db, submission)
    if base is None:
        return []
    out: list[int] = []
    for sid in await _students_on(db, submission):
        if await _is_grantable(db, submission, sid, base):
            out.append(sid)
    return out


async def grant_extra_attempt(db: AsyncSession, submission: Submission, student_id: int) -> int:
    """Give ``student_id`` one more attempt on ``submission``; returns their new extra total.

    Raises GrantError unless the pair is grantable. Does not commit — caller owns the
    transaction, as with the other teacher unstick controls.
    """
    base = await base_max_attempts(db, submission)
    if student_id not in await _students_on(db, submission) or not await _is_grantable(
        db, submission, student_id, base
    ):
        raise GrantError("student is not stuck on an exhausted quiz for this submission")
    meta = dict(submission.source_metadata or {})
    extras = dict(meta.get(EXTRA_KEY) or {})
    extras[str(student_id)] = int(extras.get(str(student_id), 0)) + 1
    meta[EXTRA_KEY] = extras
    submission.source_metadata = meta  # reassign: JSONB change tracking
    transition(submission, "quiz_attempt_granted")
    return extras[str(student_id)]
```

Check `squads.squad_for_submission` exists with signature `(db, submission)` (it is used in `student_quiz.py`) and that `Squad.members` is loaded by `_squad_query()` (it is — `member_quiz_states` reads `squad.members`).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q`
Expected: 9 passed.

- [ ] **Step 5: Lint + types**

Run: `uv run --frozen ruff check src/submissions_checker/services/quiz_grants.py tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/`
Expected: clean. If `ruff format --check` complains, run `uv run --frozen ruff format src/ tests/`.

- [ ] **Step 6: Commit**

```bash
git add src/submissions_checker/services/quiz_grants.py tests/functional/test_teacher_grant_quiz_attempt.py
git commit -m "Add grantable check and grant_extra_attempt service

'Stuck because the quiz ran out' has no marker of its own, so the
precondition reads it off the evidence: FAILED or QUIZ_SENT, a cap in
the pinned config, at least one attempt, all terminal, none passed,
used >= cap + grants. Teacher rejections cannot match (no attempts, or
a passed one), so the control never appears where it is not meant.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Teacher route with audit, notification and i18n

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (after `teacher_skip_ai_review`, ~line 1650)
- Modify: `i18n/uk.yml` (`teacher:` section near `retry_ai_review`, `quiz:` section)
- Test: `tests/functional/test_teacher_grant_quiz_attempt.py`

**Interfaces:**
- Consumes: `quiz_grants.grant_extra_attempt`, `GrantError`; `_load_submission_for_teacher`, `_board_url`, `audit`, `push_notification` (all already imported or defined in `teacher_portal.py`).
- Produces: `POST /teacher/submissions/{submission_id}/grant-quiz-attempt` with optional form `student_id`.

- [ ] **Step 1: Write the failing route tests**

Append to `tests/functional/test_teacher_grant_quiz_attempt.py`:

```python
# ── Route ────────────────────────────────────────────────────────────────────


async def test_route_grants_audits_and_notifies(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, asg, student, sa, sub = await _exhausted(db, teacher, make_student)
    user = await make_user(role=UserRole.STUDENT, username="olha", student=student)
    authenticate(client, teacher)

    r = await client.post(
        f"/teacher/submissions/{sub.id}/grant-quiz-attempt",
        data={"student_id": str(student.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/teacher/subjects/{subject.id}/assignments/{asg.id}"

    await db.refresh(sub)
    assert sub.status == SubmissionStatus.QUIZ_SENT
    assert sub.source_metadata["quiz_extra_attempts"] == {str(student.id): 1}

    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "grant_quiz_attempt"))
    ).scalar_one()
    assert log.actor_id == teacher.id
    assert log.target_type == "submission" and log.target_id == sub.id
    assert log.detail == {"student_id": student.id, "extra_attempts": 1}

    note = (
        await db.execute(select(Notification).where(Notification.user_id == user.id))
    ).scalar_one()
    assert note.title == "Додаткова спроба тесту"
    assert "L1" in note.body
    assert note.link == f"/portal/subjects/{subject.id}/assignments/{sa.id}"


async def test_route_defaults_student_to_submission_owner(
    client: AsyncClient, db, teacher, make_student
) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    authenticate(client, teacher)
    r = await client.post(f"/teacher/submissions/{sub.id}/grant-quiz-attempt", follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.source_metadata["quiz_extra_attempts"] == {str(student.id): 1}


async def test_route_refuses_when_not_grantable(
    client: AsyncClient, db, teacher, make_student
) -> None:
    _s, _a, student, _sa, sub = await _arrange(db, teacher, make_student)
    await _attempt(db, sub, student.id)  # 1 of 2 used — not exhausted
    authenticate(client, teacher)
    r = await client.post(
        f"/teacher/submissions/{sub.id}/grant-quiz-attempt",
        data={"student_id": str(student.id)},
        follow_redirects=False,
    )
    assert r.status_code == 409
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED
    assert sub.source_metadata == {}


async def test_route_other_teacher_403(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    _s, _a, student, _sa, sub = await _exhausted(db, teacher, make_student)
    other = await make_user(role=UserRole.TEACHER, username="other-t")
    authenticate(client, other)
    r = await client.post(
        f"/teacher/submissions/{sub.id}/grant-quiz-attempt",
        data={"student_id": str(student.id)},
        follow_redirects=False,
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q -k route`
Expected: 4 failed with 404 / 405 (route missing).

- [ ] **Step 3: Add vocab**

In `i18n/uk.yml`, in the `teacher:` section directly after `retry_ai_review: Повторити AI-рецензію` add:

```yaml
  grant_quiz_attempt: Додаткова спроба тесту
```

In the `quiz:` section directly after `submit_quiz: Надіслати тест` add:

```yaml
  notif_extra_attempt_title: Додаткова спроба тесту
  notif_extra_attempt_body: "Викладач надав вам ще одну спробу тесту з «{title}»."
```

- [ ] **Step 4: Add the route**

In `src/submissions_checker/api/routes/teacher_portal.py` add to the imports (alphabetically among the `services` imports):

```python
from submissions_checker.core.i18n import get_vocab
from submissions_checker.services import quiz_grants
from submissions_checker.services.notification_service import push_notification
```

(`get_vocab` may already be imported — check with `grep -n get_vocab src/submissions_checker/api/routes/teacher_portal.py` and do not duplicate.)

Then, after `teacher_skip_ai_review` (the `/send-to-teacher` route) add:

```python
@router.post("/submissions/{submission_id}/grant-quiz-attempt")
async def teacher_grant_quiz_attempt(
    submission_id: int,
    db: DBSession,
    current_user: TeacherUser,
    student_id: int | None = Form(default=None),
) -> RedirectResponse:
    """Give one student one more quiz attempt on a submission stuck on an exhausted quiz.

    ``student_id`` picks the squad member; a solo submission defaults to its owner.
    Attempt history is kept — only the allowance and the status change.
    """
    submission = await _load_submission_for_teacher(db, submission_id, current_user)
    target = student_id if student_id is not None else submission.students_assignment.student_id
    try:
        total = await quiz_grants.grant_extra_attempt(db, submission, target)
    except quiz_grants.GrantError as exc:
        raise HTTPException(
            status_code=409, detail="Submission is not in a state that allows this action"
        ) from exc

    await audit(
        db,
        action="grant_quiz_attempt",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="submission",
        target_id=submission_id,
        student_id=target,
        extra_attempts=total,
    )

    sa_cfg = submission.students_assignment.subjects_assignment
    user_id = await db.scalar(select(User.id).where(User.student_id == target))
    if user_id is not None:
        own_sa_id = await db.scalar(
            select(StudentAssignment.id).where(
                StudentAssignment.student_id == target,
                StudentAssignment.subjects_assignment_id == sa_cfg.id,
            )
        )
        vocab = get_vocab(None).get("quiz", {})
        await push_notification(
            db,
            user_id,
            str(vocab.get("notif_extra_attempt_title", "")),
            str(vocab.get("notif_extra_attempt_body", "")).format(title=sa_cfg.title),
            f"/portal/subjects/{sa_cfg.subject_id}/assignments/{own_sa_id}",
        )
    await db.commit()
    return RedirectResponse(url=_board_url(submission), status_code=303)
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q`
Expected: 13 passed.

- [ ] **Step 6: Lint + types**

Run: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/submissions_checker/api/routes/teacher_portal.py i18n/uk.yml tests/functional/test_teacher_grant_quiz_attempt.py
git commit -m "Add teacher route to grant an extra quiz attempt

Sits next to rerun-checks / retry-ai-review: same owner-or-admin load,
same 409 on a wrong state, same audit row shape so the admin audit page
lists it like every other unstick action. The student gets an in-app
notice (the precedent for teacher-triggered quiz events); no email, as
none of the unstick controls email today.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Consumers read the effective cap

**Files:**
- Modify: `src/submissions_checker/api/routes/student_quiz.py:637-648` (`_grade_and_finalize`) and `:763-771` (`start_or_resume_quiz`)
- Modify: `src/submissions_checker/api/routes/student_portal.py:355-370` (assignment detail)
- Test: `tests/functional/test_teacher_grant_quiz_attempt.py`

**Interfaces:**
- Consumes: `quiz_grants.effective_max_attempts(base, submission, student_id)`.

- [ ] **Step 1: Write the failing student-side tests**

Append to `tests/functional/test_teacher_grant_quiz_attempt.py`:

```python
# ── Student side after a grant ───────────────────────────────────────────────


async def _granted(db, client, teacher, make_user, make_student):
    subject, asg, student, sa, sub = await _exhausted(db, teacher, make_student)
    user = await make_user(role=UserRole.STUDENT, username="olha", student=student)
    await quiz_grants.grant_extra_attempt(db, sub, student.id)
    await db.commit()
    authenticate(client, user)
    return subject, asg, student, sa, sub


async def test_student_can_start_the_granted_attempt(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _a, student, sa, sub = await _granted(db, client, teacher, make_user, make_student)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303, r.text
    new_id = int(r.headers["location"].rsplit("/", 1)[-1])
    assert r.headers["location"] == f"/portal/quiz/{new_id}"
    attempt = await db.get(QuizAttempt, new_id)
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS
    assert attempt.config_snapshot["max_quiz_attempts"] == 2  # snapshot keeps the base cap


async def test_detail_page_offers_the_retry_after_grant(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _a, _st, sa, _sub = await _granted(db, client, teacher, make_user, make_student)
    page = await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")
    assert page.status_code == 200
    assert "2/3" in page.text  # used / effective cap
    assert f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz" in page.text


async def test_failing_the_granted_attempt_fails_again_with_none_left(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _a, student, sa, sub = await _granted(db, client, teacher, make_user, make_student)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    new_id = int(r.headers["location"].rsplit("/", 1)[-1])
    attempt = await db.get(QuizAttempt, new_id)
    form = {f"answer_{q['id']}": "0" for q in attempt.questions_snapshot}
    r = await client.post(f"/portal/quiz/{new_id}/submit", data=form, follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED
    # And grantable once more — each click is exactly one attempt.
    assert await quiz_grants.grantable_students(db, sub) == [student.id]
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 403


async def test_passing_the_granted_attempt_completes(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _a, _st, sa, sub = await _granted(db, client, teacher, make_user, make_student)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    new_id = int(r.headers["location"].rsplit("/", 1)[-1])
    attempt = await db.get(QuizAttempt, new_id)
    form = {f"answer_{q['id']}": "1" for q in attempt.questions_snapshot}
    r = await client.post(f"/portal/quiz/{new_id}/submit", data=form, follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q -k "granted or detail_page"`
Expected: `test_student_can_start_the_granted_attempt` fails (303 to the old result page, not a new attempt); `test_detail_page_offers_the_retry_after_grant` fails ("2/3" missing); the other two fail as a consequence. Read the output to confirm the failures are these, not setup errors.

- [ ] **Step 3: Gate in `start_or_resume_quiz`**

In `src/submissions_checker/api/routes/student_quiz.py`, add the import:

```python
from submissions_checker.services.quiz_grants import effective_max_attempts
```

Replace the "Check max attempts" block (currently):

```python
    # Check max attempts
    max_attempts = quiz_cfg.get("max_quiz_attempts")
    used_count = sum(1 for a in existing if a.status in _TERMINAL_STATUSES)
    if max_attempts is not None and used_count >= max_attempts:
```

with:

```python
    # Check max attempts — the config's cap plus anything a teacher granted this student.
    max_attempts = quiz_cfg.get("max_quiz_attempts")
    allowed = effective_max_attempts(max_attempts, latest_sub, student_id)
    used_count = sum(1 for a in existing if a.status in _TERMINAL_STATUSES)
    if allowed is not None and used_count >= allowed:
```

Leave `config_snapshot["max_quiz_attempts"] = int(max_attempts)` as is (base value).

- [ ] **Step 4: Exhaustion in `_grade_and_finalize`**

Replace:

```python
            max_attempts = attempt.config_snapshot.get("max_quiz_attempts")
            if max_attempts is not None:
                prior = await _count_used_attempts(
                    db, attempt.submission_id, attempt.id, attempt.student_id
                )
                if prior + 1 >= max_attempts:
                    transition(submission, "quiz_failed")
                    attempts_left = 0
                else:
                    attempts_left = max_attempts - (prior + 1)
```

with:

```python
            max_attempts = effective_max_attempts(
                attempt.config_snapshot.get("max_quiz_attempts"), submission, attempt.student_id
            )
            if max_attempts is not None:
                prior = await _count_used_attempts(
                    db, attempt.submission_id, attempt.id, attempt.student_id
                )
                if prior + 1 >= max_attempts:
                    transition(submission, "quiz_failed")
                    attempts_left = 0
                else:
                    attempts_left = max_attempts - (prior + 1)
```

`attempt.student_id` may be `None` on legacy rows; `effective_max_attempts` accepts that because `extra_attempts` looks up `str(None)` and gets 0. Type it as `int | None` on the helper's `student_id` parameter — update `services/quiz_grants.py` signatures of `extra_attempts` and `effective_max_attempts` to `student_id: int | None`.

- [ ] **Step 5: Student assignment detail**

In `src/submissions_checker/api/routes/student_portal.py` add the import:

```python
from submissions_checker.services.quiz_grants import effective_max_attempts
```

After the block that fills `quiz_max_attempts` (ends with `quiz_max_attempts = quiz_cfg.get("max_quiz_attempts")`, just before `check_reason: str | None = None`), add:

```python
    if latest_sub is not None:
        quiz_max_attempts = effective_max_attempts(quiz_max_attempts, latest_sub, student_id)
```

- [ ] **Step 6: Run the file and the existing quiz suites**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py tests/functional/test_student_quiz.py tests/functional/test_squad_quiz.py tests/functional/test_student_portal.py tests/functional/test_student_quiz_grading_branches.py -q`
Expected: all pass.

- [ ] **Step 7: Lint + types**

Run: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/submissions_checker/api/routes/student_quiz.py src/submissions_checker/api/routes/student_portal.py src/submissions_checker/services/quiz_grants.py tests/functional/test_teacher_grant_quiz_attempt.py
git commit -m "Honour granted quiz attempts in the gate, finalizer and detail page

The attempt snapshot keeps recording the config's base cap; the extra
is added at read time so a second grant cannot double-count a first one
that was already baked into a snapshot. Failing the granted attempt
re-fails the submission with none left, so one click is one attempt.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Board button

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py:531-540` (in `teacher_assignment`, after `stuck_ids`)
- Modify: `templates/teacher_assignment.html:151-158` (status cell)
- Test: `tests/functional/test_teacher_grant_quiz_attempt.py`

**Interfaces:**
- Consumes: `quiz_grants.grantable_students(db, submission)`.
- Produces: template context `grantable: set[tuple[int, int]]` of `(submission_id, student_id)`.

- [ ] **Step 1: Write the failing board test**

Append to `tests/functional/test_teacher_grant_quiz_attempt.py`:

```python
# ── Board ────────────────────────────────────────────────────────────────────


async def test_board_shows_button_only_for_grantable_rows(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, exhausted, _sa, sub = await _exhausted(db, teacher, make_student)
    # A second student on the same assignment, failed with 1 of 2 used — not grantable.
    other = await make_student(full_name="Petro P")
    db.add(SubjectsStudents(subject_id=subject.id, student_id=other.id))
    sa2 = StudentAssignment(student_id=other.id, subjects_assignment_id=asg.id)
    db.add(sa2)
    await db.commit()
    await db.refresh(sa2)
    sub2 = Submission(
        students_assignment_id=sa2.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=SubmissionStatus.FAILED,
        plugin_config_id=sub.plugin_config_id,
    )
    db.add(sub2)
    await db.commit()
    await db.refresh(sub2)
    await _attempt(db, sub2, other.id)

    authenticate(client, teacher)
    page = await client.get(f"/teacher/subjects/{subject.id}/assignments/{asg.id}")
    assert page.status_code == 200
    assert "Додаткова спроба тесту" in page.text
    assert f'action="/teacher/submissions/{sub.id}/grant-quiz-attempt"' in page.text
    assert f'name="student_id" value="{exhausted.id}"' in page.text
    assert f'action="/teacher/submissions/{sub2.id}/grant-quiz-attempt"' not in page.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py -q -k board`
Expected: FAIL on the "Додаткова спроба тесту" assertion.

- [ ] **Step 3: Compute `grantable` in the route**

In `teacher_assignment` (`teacher_portal.py`), right after the `stuck_ids = {...}` block, add:

```python
    # Students stuck on an exhausted quiz get the "one more attempt" control. Only FAILED /
    # QUIZ_SENT rows can qualify, and a squad's rows share one submission, so load each
    # candidate submission once and key the result by (submission, student).
    candidate_ids = {
        r["submission_id"]
        for r in rows
        if r["submission_id"]
        and r["submission_status"] in (SubmissionStatus.FAILED, SubmissionStatus.QUIZ_SENT)
    }
    grantable: set[tuple[int, int]] = set()
    if candidate_ids:
        candidates = (
            (await db.execute(select(Submission).where(Submission.id.in_(candidate_ids))))
            .scalars()
            .all()
        )
        for cand in candidates:
            for sid in await quiz_grants.grantable_students(db, cand):
                grantable.add((cand.id, sid))
```

and add `"grantable": grantable,` to the `render(...)` context dict of that route.

- [ ] **Step 4: Render the button**

In `templates/teacher_assignment.html`, directly after the `{% endif %}` that closes the `{% if st == "AI_REVIEW_FAILED" %}` block (before `</td>`), add:

```html
          {% if grantable is defined and (row.submission_id, row.student_id) in grantable %}
          <form method="POST" action="/teacher/submissions/{{ row.submission_id }}/grant-quiz-attempt" class="inline">
            <input type="hidden" name="student_id" value="{{ row.student_id }}">
            <button type="submit" class="ml-1.5 text-xs text-slate-500 hover:text-indigo-700 underline underline-offset-2">{{ vocab.teacher.grant_quiz_attempt }}</button>
          </form>
          {% endif %}
```

- [ ] **Step 5: Run the board test and the teacher portal suites**

Run: `uv run --frozen --extra dev pytest tests/functional/test_teacher_grant_quiz_attempt.py tests/functional/test_teacher_portal.py tests/functional/test_teacher_portal_deep.py tests/functional/test_teacher_squad_views.py -q`
Expected: all pass.

- [ ] **Step 6: Lint + types**

Run: `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/submissions_checker/api/routes/teacher_portal.py templates/teacher_assignment.html tests/functional/test_teacher_grant_quiz_attempt.py
git commit -m "Show the extra-attempt control on the assignment board

Placed in the status cell beside rerun-checks / retry-AI so every
unstick action lives in one spot. Keyed by (submission, student)
because a squad's members each have a row on the shared submission and
only the exhausted member should get the button.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Squad behaviour

**Files:**
- Test: `tests/functional/test_squad_quiz.py`

**Interfaces:**
- Consumes: `_arrange`, `_lock_pair`, `_client`, `_upload`, `_start`, `_answer_all` already in that file; `quiz_grants.grant_extra_attempt`, `quiz_grants.grantable_students`.

- [ ] **Step 1: Write the squad tests**

Append to `tests/functional/test_squad_quiz.py`:

```python
async def test_grant_reopens_the_squad_quiz_for_the_exhausted_member_only(
    db, make_user, make_student, teacher
) -> None:
    from submissions_checker.services import quiz_grants

    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_a = await _start(ca, subject, sa_a)
        await _answer_all(ca, db, att_a, correct=True)  # A passes their half
        for _ in range(2):
            att = await _start(cb, subject, sa_b)
            await _answer_all(cb, db, att, correct=False)  # B exhausts
        sub = (await db.execute(select(Submission))).scalar_one()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.FAILED
        assert await quiz_grants.grantable_students(db, sub) == [ub.student_id]

        await quiz_grants.grant_extra_attempt(db, sub, ub.student_id)
        await db.commit()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.QUIZ_SENT

        # A's passed attempt is untouched; A's quiz link still lands on their result.
        a_row = await db.get(QuizAttempt, att_a)
        assert a_row.is_passed is True
        r = await ca.get(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/quiz", follow_redirects=False
        )
        assert r.headers["location"].endswith(f"/portal/quiz/{att_a}/result")

        # B gets a fresh attempt of their own slice size and, passing it, completes the squad.
        att_b3 = await _start(cb, subject, sa_b)
        b3 = await db.get(QuizAttempt, att_b3)
        assert b3.status == QuizAttemptStatus.IN_PROGRESS
        assert len(b3.questions_snapshot) == 2
        await _answer_all(cb, db, att_b3, correct=True)
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW  # quiz_then_teacher


async def test_second_exhausted_member_is_grantable_while_quiz_sent(
    db, make_user, make_student, teacher
) -> None:
    """A finished exhausting while B was mid-attempt; B then exhausted on a FAILED
    submission. Granting A moves it to QUIZ_SENT; B must still be grantable there."""
    from submissions_checker.services import quiz_grants

    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await _upload(ca, subject, sa_a)
        att_b1 = await _start(cb, subject, sa_b)
        await _answer_all(cb, db, att_b1, correct=False)
        att_b2 = await _start(cb, subject, sa_b)  # B mid-attempt
        att_a1 = await _start(ca, subject, sa_a)
        await _answer_all(ca, db, att_a1, correct=False)
        att_a2 = await _start(ca, subject, sa_a)
        await _answer_all(ca, db, att_a2, correct=False)  # A exhausts → FAILED
        await _answer_all(cb, db, att_b2, correct=False)  # B's late finish: also exhausted
        sub = (await db.execute(select(Submission))).scalar_one()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.FAILED
        assert await quiz_grants.grantable_students(db, sub) == sorted(
            [ua.student_id, ub.student_id]
        )

        await quiz_grants.grant_extra_attempt(db, sub, ua.student_id)
        await db.commit()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.QUIZ_SENT
        assert await quiz_grants.grantable_students(db, sub) == [ub.student_id]

        await quiz_grants.grant_extra_attempt(db, sub, ub.student_id)  # self-loop edge
        await db.commit()
        await db.refresh(sub)
        assert sub.status == SubmissionStatus.QUIZ_SENT
        assert sub.source_metadata["quiz_extra_attempts"] == {
            str(ua.student_id): 1,
            str(ub.student_id): 1,
        }
        assert await quiz_grants.grantable_students(db, sub) == []
```

- [ ] **Step 2: Run them**

Run: `uv run --frozen --extra dev pytest tests/functional/test_squad_quiz.py -q`
Expected: all pass (these exercise Tasks 1-5; if a squad test fails, the bug is in the service or the gate, not in the test — read the assertion that failed). Note `_start` asserts a 303: after the grant B's link must create an attempt, so a 403 here means Task 5's gate is not reading the extra.

- [ ] **Step 3: Commit**

```bash
git add tests/functional/test_squad_quiz.py
git commit -m "Cover extra quiz attempts on squad-shared submissions

Pins two rules from docs/features/squads.md: a grant targets the member
who exhausted and leaves a partner's pass alone, and a second member
who exhausted late is still grantable once the first grant has already
moved the submission back to QUIZ_SENT.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Docs

**Files:**
- Modify: `docs/feature_catalog.md:103` (route table) and `:110-128` (state diagram)
- Modify: `docs/features/squads.md:148` (edge-case table) and the Routes table
- Modify: `docs/teacher_journey_guide.md:318-322` (unstick list)

- [ ] **Step 1: Feature catalog**

After the `retry-ai-review` row in the route table add:

```markdown
| Grant one more quiz attempt to a student who exhausted `max_quiz_attempts` (from `FAILED`, or `QUIZ_SENT` for a second squad member; attempt history kept; student notified in-app; audited `grant_quiz_attempt`) | Owner / ADMIN | `POST /teacher/submissions/{id}/grant-quiz-attempt` (form `student_id`) |
```

In the state diagram after `                        ──teacher_send_quiz──▶ QUIZ_SENT` add:

```
QUIZ_SENT ──quiz_passed──▶ COMPLETED
          ──quiz_passed_teacher──▶ AWAITING_TEACHER_REVIEW
          ──quiz_failed──▶ FAILED
          ──quiz_attempt_granted──▶ QUIZ_SENT   (squad: a second exhausted member)
FAILED ──quiz_attempt_granted──▶ QUIZ_SENT      (teacher grants one more attempt)
       ──dispute_regrade_passed──▶ COMPLETED
       ──dispute_regrade_passed_teacher──▶ AWAITING_TEACHER_REVIEW
```

- [ ] **Step 2: Squads doc**

Replace the edge-case row

```markdown
| A member exhausts quiz attempts without passing | Submission goes `FAILED` (whole squad); members who already passed keep their attempt rows. |
```

with

```markdown
| A member exhausts quiz attempts without passing | Submission goes `FAILED` (whole squad); members who already passed keep their attempt rows. |
| Teacher wants to give an exhausted member one more try | The board shows **Додаткова спроба тесту** on that member's row only. Granting it (`POST /teacher/submissions/{id}/grant-quiz-attempt`, `student_id` = the member) records `+1` for that member in `submissions.source_metadata.quiz_extra_attempts` and moves the submission back to `QUIZ_SENT`; partners' passed attempts are untouched and the stored draw is reused. If a second member also exhausted (they were mid-attempt when the first one failed the squad), their button stays available on the now-`QUIZ_SENT` submission. |
```

In the Routes table add:

```markdown
| `POST /teacher/submissions/{id}/grant-quiz-attempt` | Owner / ADMIN | `grant_quiz_attempt` |
```

- [ ] **Step 3: Teacher journey guide**

In the unstick list, after the **Повторити AI-рецензію** bullet add:

```markdown
- **Додаткова спроба тесту** on a `FAILED` row whose student used every `max_quiz_attempts`
  without passing: gives that one student one more attempt (click again for another), keeps
  every previous attempt on record, and sends them an in-app notice. Not offered on
  submissions you rejected yourself or that already completed.
```

and change `Every action is audited (`rerun_checks`, `retry_ai_review`, `ai_review_skip_to_teacher`).` to include `grant_quiz_attempt`.

- [ ] **Step 4: Commit**

```bash
git add docs/feature_catalog.md docs/features/squads.md docs/teacher_journey_guide.md
git commit -m "Document the extra quiz attempt control

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Full verification

- [ ] **Step 1: Full suite, lint, types**

Run:

```bash
uv run --frozen --extra dev pytest -q 2>&1 | tail -15
uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy src/
git status --short   # uv.lock must not appear
```

Expected: pytest summary line with 0 failed; ruff and mypy clean; `uv.lock` unchanged.

- [ ] **Step 2: Fix anything red, re-run, then report**

Do not report done until Step 1's output has been read and shows green.
