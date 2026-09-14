"""Coverage for ``student_quiz._grade_answer`` fallback branches.

The submit handler (``POST /portal/quiz/{id}/submit``) reads answers straight
from the HTTP form, so values are always strings (or lists of strings). When a
student posts malformed answers, ``_grade_answer`` must degrade gracefully:

* MULTIPLE_CHOICE with a non-numeric option value  → ``selected = []`` (202-204)
* ORDERING with a non-numeric token                → ``order = []``    (212-213)
* an unknown question type in the snapshot         → ``{"raw": ...}``  (226)

These are exercised end-to-end through the real submit route with crafted
question snapshots + form bodies (so they count as functional coverage), and the
pure ``_grade_answer`` helper is also unit-checked for the list-coercion branch
that the route cannot reach (``form.getlist`` always returns a list, so the
``elif raw_answer`` single-value coercion at lines 197-198 is only reachable by
a direct call).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes.student_quiz import _grade_answer
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
from submissions_checker.db.models.quiz_template import QuizAnswer
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` — pytest is in
# ``asyncio_mode = "auto"`` so coroutine tests run automatically, and the pure
# (sync) ``_grade_answer`` unit checks below stay sync without a spurious
# "marked asyncio but not async" warning.


# ── Pure-helper unit checks (deterministic, no DB) ──────────────────────────


def test_grade_multiple_choice_non_numeric_yields_empty_selected() -> None:
    # int("abc") raises ValueError → selected = [] (lines 201-204).
    q = {"type": "MULTIPLE_CHOICE", "config": {"correct": [0, 1]}, "points": 2}
    answer, is_correct, points = _grade_answer(q, ["abc", "xyz"])
    assert answer == {"selected": []}
    assert is_correct is False
    assert points == 0


def test_grade_multiple_choice_scalar_truthy_is_coerced_to_list() -> None:
    # NOTE: lines 197-198 (``elif raw_answer: values = [raw_answer]``) are NOT
    # reachable through the submit route, which always passes a list from
    # ``form.getlist``. Cover via a direct call with a scalar string.
    q = {"type": "MULTIPLE_CHOICE", "config": {"correct": [1]}, "points": 1}
    answer, is_correct, points = _grade_answer(q, "1")
    assert answer == {"selected": [1]}
    assert is_correct is True
    assert points == 1


def test_grade_multiple_choice_empty_falsy_yields_empty() -> None:
    q = {"type": "MULTIPLE_CHOICE", "config": {"correct": [0]}, "points": 1}
    answer, is_correct, points = _grade_answer(q, "")
    assert answer == {"selected": []}
    assert is_correct is False


def test_grade_ordering_non_numeric_yields_empty_order() -> None:
    # int("a") raises ValueError → order = [] (lines 210-213).
    q = {"type": "ORDERING", "config": {"correct_order": [0, 1, 2]}, "points": 3}
    answer, is_correct, points = _grade_answer(q, "a,b,c")
    assert answer == {"order": []}
    assert is_correct is False
    assert points == 0


def test_grade_unknown_question_type_falls_through() -> None:
    # A type not in the known set hits the final fallback (line 226).
    q = {"type": "ESSAY", "config": {}, "points": 5}
    answer, is_correct, points = _grade_answer(q, "free text")
    assert answer == {"raw": "free text"}
    assert is_correct is False
    assert points == 0


# ── End-to-end submit-route coverage of the same fallback branches ──────────


async def _arrange_in_progress_attempt(
    db,
    student_id: int,
    questions_snapshot: list,
) -> QuizAttempt:
    subject = Subject(name="GradeBranches")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"h-{subject.id}",
        config={"assignments": {"hw1": {"quiz": {}}}},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    sub_a = SubjectsAssignment(subject_id=subject.id, title="HW1", code="hw1", config={})
    db.add(sub_a)
    await db.commit()
    await db.refresh(sub_a)

    sa = StudentAssignment(student_id=student_id, subjects_assignment_id=sub_a.id)
    db.add(sa)
    await db.commit()
    await db.refresh(sa)

    submission = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=SubmissionStatus.QUIZ_SENT,
        plugin_config_id=cfg.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    attempt = QuizAttempt(
        submission_id=submission.id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=questions_snapshot,
        config_snapshot={"pass_threshold_pct": 0.6},
        started_at=datetime.now(UTC),
        status=QuizAttemptStatus.IN_PROGRESS,
        violations={},
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def test_submit_with_malformed_answers_grades_to_zero(
    student_client: AsyncClient, db, student_user
) -> None:
    # One MULTIPLE_CHOICE, one ORDERING, one unknown-type question; all posted
    # with non-numeric / freeform values. The submit route must finalize the
    # attempt (303 → result) with every answer graded to 0.
    snapshot = [
        {
            "id": 0,
            "type": "MULTIPLE_CHOICE",
            "text": "pick",
            "points": 2,
            "is_required": False,
            "config": {"options": ["a", "b"], "correct": [0]},
        },
        {
            "id": 1,
            "type": "ORDERING",
            "text": "order",
            "points": 3,
            "is_required": False,
            "config": {"correct_order": [0, 1]},
        },
        {
            "id": 2,
            "type": "ESSAY",
            "text": "essay",
            "points": 5,
            "is_required": False,
            "config": {},
        },
    ]
    attempt = await _arrange_in_progress_attempt(db, student_user.student_id, snapshot)

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={
            "answer_0": "not-a-number",  # MULTIPLE_CHOICE → getlist → ["not-a-number"]
            "answer_ordering_1": "x,y,z",  # ORDERING → non-numeric tokens
            "answer_2": "some prose",  # unknown type → raw fallback
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"

    answers = (
        (
            await db.execute(
                select(QuizAnswer)
                .where(QuizAnswer.attempt_id == attempt.id)
                .order_by(QuizAnswer.question_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(answers) == 3
    mc, ordering, essay = answers
    assert mc.answer == {"selected": []}
    assert mc.points_earned == 0
    assert ordering.answer == {"order": []}
    assert ordering.points_earned == 0
    assert essay.answer == {"raw": "some prose"}
    assert essay.points_earned == 0

    # Attempt finalized as COMPLETED with a zero score.
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.score == 0
    assert attempt.is_passed is False
