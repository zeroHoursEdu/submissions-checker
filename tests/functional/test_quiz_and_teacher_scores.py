"""quiz_and_teacher_scores: the quiz opens without an upload, the teacher enters points.

Service-level tests first (real Postgres, no HTTP), then the student and teacher routes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from submissions_checker.db.models import (
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
)
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services.grading import finalize_grade

pytestmark = pytest.mark.asyncio

MODE = "quiz_and_teacher_scores"
GRADING: dict[str, Any] = {
    "quiz_points": 8,
    "teacher_criteria": [
        {"key": "report", "title": "Звіт", "max": 5},
        {"key": "star", "title": "Зірочка", "max": 3, "optional": True},
    ],
}
QUIZ: dict[str, Any] = {
    "questions": [
        {"type": "single_choice", "text": "q", "points": 1, "options": ["w", "r"], "correct": 1}
    ],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.6,
    "max_quiz_attempts": 2,
}


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _arrange_scored_no_sub(db, teacher, make_student, *, mode: str = MODE, scores=None):
    """Owned subject with one scored assignment and its plugin config, one enrolled student
    with consent and an SA row (``teacher_scores`` = ``scores``), no submission.
    Returns (subject, assignment, student, sa)."""
    subject = Subject(name="Scoreland", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    a_cfg = {"review_mode": mode, "grading": GRADING, "quiz": QUIZ}
    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"h{subject.id}",
        config={"assignments": {"l1": a_cfg}},
    )
    asg = SubjectsAssignment(
        subject_id=subject.id,
        title="L1",
        code="l1",
        min_grade=0,
        max_grade=16,
        config={"review_mode": mode, "grading": GRADING},
    )
    db.add_all([cfg, asg])
    await db.commit()
    await db.refresh(asg)
    student = await make_student(full_name="Valentyn D")
    student.recording_consent_at = datetime.now(UTC)
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    sa = StudentAssignment(
        student_id=student.id, subjects_assignment_id=asg.id, teacher_scores=scores
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return subject, asg, student, sa


async def _arrange_scored(
    db,
    teacher,
    make_student,
    *,
    status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
    scores=None,
    source_type: SubmissionSourceType = SubmissionSourceType.QUIZ_ONLY,
):
    """As ``_arrange_scored_no_sub`` plus one submission in ``status``.
    Returns (subject, assignment, student, sa, submission)."""
    subject, asg, student, sa = await _arrange_scored_no_sub(
        db, teacher, make_student, scores=scores
    )
    config_id = (
        await db.execute(
            SubjectPluginConfig.__table__.select().where(
                SubjectPluginConfig.subject_id == subject.id
            )
        )
    ).first()[0]
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=source_type,
        source_metadata={}
        if source_type == SubmissionSourceType.QUIZ_ONLY
        else {"saved_as": "x.zip"},
        status=status,
        plugin_config_id=config_id,
        test_results={"skipped": True, "reason": MODE},
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return subject, asg, student, sa, sub


async def _attempt(
    db,
    sub,
    student_id,
    *,
    is_passed: bool = False,
    score: int | None = None,
    max_score: int = 1,
    review_mode: str = MODE,
    status: QuizAttemptStatus = QuizAttemptStatus.COMPLETED,
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
        config_snapshot={
            "pass_threshold_pct": 0.6,
            "max_quiz_attempts": 2,
            "review_mode": review_mode,
        },
        started_at=datetime.now(UTC),
        submitted_at=datetime.now(UTC),
        status=status,
        is_passed=is_passed,
        score=score if score is not None else (max_score if is_passed else 0),
        max_score=max_score,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


# ── Schema ───────────────────────────────────────────────────────────────────


async def test_quiz_only_submission_and_teacher_scores_round_trip(
    db, teacher, make_student
) -> None:
    *_, sa, sub = await _arrange_scored(db, teacher, make_student, scores={"report": 4})
    await db.refresh(sa)
    await db.refresh(sub)
    assert sub.source_type == SubmissionSourceType.QUIZ_ONLY
    assert sa.teacher_scores == {"report": 4}


# ── finalize_grade ───────────────────────────────────────────────────────────


async def test_finalize_scored_writes_sum_and_breakdown(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(
        db,
        teacher,
        make_student,
        status=SubmissionStatus.COMPLETED,
        scores={"report": 4, "star": 2},
    )
    await _attempt(db, sub, sa.student_id, is_passed=True, score=3, max_score=4)  # 75%
    await finalize_grade(db, sub)
    await db.commit()
    await db.refresh(sa)
    await db.refresh(sub)
    assert sa.grade == 12
    assert sub.grade_breakdown["quiz"] == {"pct": 75.0, "points": 6, "max": 8}
    assert sub.grade_breakdown["criteria"][0] == {
        "key": "report",
        "title": "Звіт",
        "points": 4,
        "max": 5,
    }


async def test_finalize_scored_noop_without_required_points(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.COMPLETED, scores={"star": 2}
    )
    await _attempt(db, sub, sa.student_id, is_passed=True)
    assert await finalize_grade(db, sub) is None
    await db.commit()
    await db.refresh(sa)
    assert sa.grade is None


async def test_finalize_scored_noop_without_passed_quiz(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.COMPLETED, scores={"report": 5}
    )
    await _attempt(db, sub, sa.student_id, is_passed=False)
    assert await finalize_grade(db, sub) is None
