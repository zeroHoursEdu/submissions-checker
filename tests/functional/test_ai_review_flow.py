"""Functional coverage for the AI-review gate and its student-facing display.

Exercises ``execute_ai_review_task`` with a mocked provider (no network), and
the assignment-detail page's AI-comment / grade-breakdown visibility toggles.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from submissions_checker.db.models import (
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.services.ai.provider import AIProviderError
from submissions_checker.workers.tasks import review_tasks

CLEAN_VERDICT = {
    "cheating": {"is_cheating": False, "confidence": 0.1, "reason": "original"},
    "ai_generated": {"is_ai_generated": False, "confidence": 0.2, "reason": "human"},
    "code_mark": 82,
    "comment": "Nice work — clear structure.",
}
FLAGGED_VERDICT = {
    "cheating": {"is_cheating": True, "confidence": 0.9, "reason": "copied"},
    "ai_generated": {"is_ai_generated": False, "confidence": 0.1, "reason": "n/a"},
    "code_mark": 40,
    "comment": "This looks copied.",
}


class _FakeProvider:
    name = "openai"
    model = "gpt-test"

    def __init__(self, verdict: dict | object) -> None:  # type: ignore[type-arg]
        self._verdict = verdict

    async def review(self, system: str, user: str, schema: dict) -> dict:  # type: ignore[type-arg]
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict  # type: ignore[return-value]


def _patch_provider(monkeypatch: pytest.MonkeyPatch, verdict: object) -> None:
    monkeypatch.setattr(
        review_tasks, "get_ai_provider", lambda settings=None: _FakeProvider(verdict)
    )


async def _arrange_submission(
    db, student_id: int, *, config: dict, status: SubmissionStatus, test_results: dict | None = None
) -> tuple[Submission, int, int]:  # type: ignore[type-arg]
    """Build the full graph; returns (submission, subject_id, student_assignment_id)."""
    subject = Subject(name="AIReview")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    subject_id = subject.id

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject_id))
    sub_a = SubjectsAssignment(
        subject_id=subject_id,
        title="Lab AI",
        code="lab_ai",
        config=config,
        min_grade=0,
        max_grade=100,
    )
    db.add(sub_a)
    await db.commit()
    await db.refresh(sub_a)

    sa = StudentAssignment(student_id=student_id, subjects_assignment_id=sub_a.id)
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    sa_id = sa.id

    submission = Submission(
        students_assignment_id=sa_id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=status,
        test_results=test_results,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return submission, subject_id, sa_id


async def test_clean_verdict_advances_to_quiz(
    db, student_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, CLEAN_VERDICT)
    sub, _, _ = await _arrange_submission(
        db,
        student_user.student_id,
        config={"ai_review": {}},
        status=SubmissionStatus.AWAITING_AI_REVIEW,
    )
    await review_tasks.execute_ai_review_task(db, {"submission_id": sub.id, "next_step": "quiz"})
    await db.commit()
    assert sub.status == SubmissionStatus.QUIZ_SENT
    assert sub.ai_review["code_mark"] == 82
    assert sub.ai_review["provider"] == "openai"


async def test_flagged_verdict_escalates_to_teacher(
    db, student_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, FLAGGED_VERDICT)
    sub, _, _ = await _arrange_submission(
        db,
        student_user.student_id,
        config={"ai_review": {"cheating_threshold": 0.6}},
        status=SubmissionStatus.AWAITING_AI_REVIEW,
    )
    await review_tasks.execute_ai_review_task(db, {"submission_id": sub.id, "next_step": "quiz"})
    await db.commit()
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    # code_mark still recorded on the flagged path
    assert sub.ai_review["code_mark"] == 40


async def test_malformed_verdict_fails_review(
    db, student_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, {"comment": "missing everything else"})
    sub, _, _ = await _arrange_submission(
        db,
        student_user.student_id,
        config={"ai_review": {}},
        status=SubmissionStatus.AWAITING_AI_REVIEW,
    )
    with pytest.raises(AIProviderError):
        await review_tasks.execute_ai_review_task(
            db, {"submission_id": sub.id, "next_step": "quiz"}
        )
    assert sub.status == SubmissionStatus.AI_REVIEW_FAILED


async def test_completed_path_finalizes_grade(
    db, student_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_provider(monkeypatch, CLEAN_VERDICT)
    # code_weight uses works (test %) and quality (AI mark).
    grading = {
        "code_weight": 1.0,
        "quiz_weight": 0.0,
        "code": {"works_weight": 0.5, "quality_weight": 0.5},
    }
    sub, _, sa_id = await _arrange_submission(
        db,
        student_user.student_id,
        config={"ai_review": {}, "grading": grading},
        status=SubmissionStatus.AWAITING_AI_REVIEW,
        test_results={"score": 10, "max_score": 10},  # works = 100
    )
    await review_tasks.execute_ai_review_task(
        db, {"submission_id": sub.id, "next_step": "completed"}
    )
    await db.commit()
    assert sub.status == SubmissionStatus.COMPLETED
    sa = await db.get(StudentAssignment, sa_id)
    # grade = 0.5*100 + 0.5*82 = 91
    assert sa.grade == 91
    assert sub.grade_breakdown["quality_score"] == 82


# ── Student-facing display toggles ─────────────────────────────────────────────


async def _completed_submission_with_review(
    db, student_id: int, *, show_comment: bool, show_breakdown: bool
) -> tuple[int, int]:
    config = {
        "ai_review": {"show_comment_to_student": show_comment},
        "grading": {"show_breakdown_to_student": show_breakdown},
    }
    sub, subject_id, sa_id = await _arrange_submission(
        db,
        student_id,
        config=config,
        status=SubmissionStatus.COMPLETED,
        test_results={"score": 8, "max_score": 10},
    )
    sub.ai_review = dict(CLEAN_VERDICT, provider="openai", model="gpt-test")
    sub.grade_breakdown = {
        "grade": 80,
        "works_score": 80.0,
        "quality_score": 82.0,
        "quiz_score": None,
        "code_weight": 1.0,
        "quiz_weight": 0.0,
        "works_weight": 1.0,
        "quality_weight": 0.0,
    }
    await db.commit()
    return sa_id, subject_id


async def test_detail_shows_comment_and_breakdown_when_enabled(
    student_client: AsyncClient, db, student_user
) -> None:
    sa_id, subject_id = await _completed_submission_with_review(
        db, student_user.student_id, show_comment=True, show_breakdown=True
    )
    resp = await student_client.get(f"/portal/subjects/{subject_id}/assignments/{sa_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Nice work" in body
    assert "How this grade was calculated" in body


async def test_detail_hides_comment_and_breakdown_when_disabled(
    student_client: AsyncClient, db, student_user
) -> None:
    sa_id, subject_id = await _completed_submission_with_review(
        db, student_user.student_id, show_comment=False, show_breakdown=False
    )
    resp = await student_client.get(f"/portal/subjects/{subject_id}/assignments/{sa_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Nice work" not in body
    assert "How this grade was calculated" not in body
