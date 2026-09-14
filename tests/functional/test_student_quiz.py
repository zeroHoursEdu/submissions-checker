"""Functional coverage for the STUDENT quiz flow (``/portal/quiz/...``).

Focus: consent gating for proctored quizzes, ownership of attempts (a student
cannot view/submit another student's attempt), the attempt lifecycle, the
multi-attempt limit, time-limit timeout, violation handling, pass threshold,
and result visibility. Behavior is asserted against the real handlers in
``api/routes/student_quiz.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

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

pytestmark = pytest.mark.asyncio


# ── Quiz config used across tests ────────────────────────────────────────────
# Two SINGLE_CHOICE questions worth 1 point each, no shuffling so question ids
# (0-based config indices) and option order are deterministic.

QUIZ_CONFIG = {
    "questions": [
        {
            "type": "single_choice",
            "text": "2 + 2 = ?",
            "points": 1,
            "options": ["3", "4", "5"],
            "correct": 1,
        },
        {
            "type": "single_choice",
            "text": "Sky color?",
            "points": 1,
            "options": ["green", "blue"],
            "correct": 1,
        },
    ],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.6,
    "show_correct_answers_after": True,
}


# ── Arrangement helpers ──────────────────────────────────────────────────────


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


async def _arrange_quiz(
    db,
    student_id: int,
    *,
    quiz_cfg: dict | None = None,
    assignment_code: str = "hw1",
    submission_status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
) -> tuple[Subject, StudentAssignment, Submission, SubjectPluginConfig]:
    """Build a fully-pinned QUIZ_SENT submission ready for ``start_or_resume_quiz``."""
    subject = Subject(name="Quizland")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-{subject.id}",
        config={"assignments": {assignment_code: {"quiz": quiz_cfg or QUIZ_CONFIG}}},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    sub_a = SubjectsAssignment(subject_id=subject.id, title="HW1", code=assignment_code, config={})
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
        status=submission_status,
        plugin_config_id=cfg.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    return subject, sa, submission, cfg


async def _make_attempt(
    db,
    submission_id: int,
    cfg: SubjectPluginConfig,
    *,
    questions_snapshot: list | None = None,
    config_snapshot: dict | None = None,
    status: QuizAttemptStatus = QuizAttemptStatus.IN_PROGRESS,
    started_at: datetime | None = None,
    violations: dict | None = None,
    is_passed: bool | None = None,
) -> QuizAttempt:
    snap = (
        questions_snapshot
        if questions_snapshot is not None
        else [
            {
                "id": 0,
                "type": "SINGLE_CHOICE",
                "text": "2+2",
                "points": 1,
                "is_required": False,
                "config": {"options": ["3", "4", "5"], "correct": 1},
            },
            {
                "id": 1,
                "type": "SINGLE_CHOICE",
                "text": "sky",
                "points": 1,
                "is_required": False,
                "config": {"options": ["green", "blue"], "correct": 1},
            },
        ]
    )
    attempt = QuizAttempt(
        submission_id=submission_id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=snap,
        config_snapshot=config_snapshot or {"pass_threshold_pct": 0.6},
        started_at=started_at or datetime.now(UTC),
        status=status,
        violations=violations or {},
        is_passed=is_passed,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


# ── Consent gating on proctored quiz ─────────────────────────────────────────


async def test_start_quiz_redirects_to_consent_when_unconsented(
    student_client: AsyncClient, db, student_user
) -> None:
    subject, sa, _sub, _cfg = await _arrange_quiz(db, student_user.student_id)
    # No consent set.
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/consent"


async def test_show_quiz_redirects_to_consent_when_unconsented(
    student_client: AsyncClient, db, student_user
) -> None:
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/consent"


# ── Attempt lifecycle (start / resume / passed redirect) ─────────────────────


async def test_start_quiz_creates_in_progress_attempt(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, sub, _cfg = await _arrange_quiz(db, student_user.student_id)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/portal/quiz/")

    attempts = (
        (await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].status == QuizAttemptStatus.IN_PROGRESS
    assert len(attempts[0].questions_snapshot) == 2


async def test_start_quiz_resumes_existing_in_progress(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    existing = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{existing.id}"
    # No second attempt was created.
    attempts = (
        (await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(attempts) == 1


async def test_start_quiz_redirects_passed_attempt_to_result(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    passed = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=True
    )
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{passed.id}/result"


async def test_start_quiz_requires_quiz_sent_status(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, _sub, _cfg = await _arrange_quiz(
        db, student_user.student_id, submission_status=SubmissionStatus.PENDING
    )
    resp = await student_client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz")
    assert resp.status_code == 403


# ── Multi-attempt limit ──────────────────────────────────────────────────────


async def test_start_quiz_max_attempts_redirects_to_last_result(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    cfg_with_max = {**QUIZ_CONFIG, "max_quiz_attempts": 2}
    subject, sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, quiz_cfg=cfg_with_max)
    # Two finished (failed) attempts already used.
    await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=False)
    last = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.TIMED_OUT, is_passed=False)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{last.id}/result"
    # No new attempt created.
    attempts = (
        (await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id)))
        .scalars()
        .all()
    )
    assert len(attempts) == 2


# ── Ownership of attempts (cross-student) ────────────────────────────────────


async def test_view_other_students_attempt_403(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    await _consent(db, other.id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert resp.status_code == 403


async def test_submit_other_students_attempt_403(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    _subject, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.post(f"/portal/quiz/{attempt.id}/submit", data={})
    assert resp.status_code == 403


async def test_result_of_other_students_attempt_403(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    _subject, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=True
    )
    resp = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert resp.status_code == 403


async def test_violation_event_other_students_attempt_403(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    _subject, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
    )
    assert resp.status_code == 403


async def test_missing_attempt_404(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    resp = await student_client.get("/portal/quiz/999999", follow_redirects=False)
    assert resp.status_code == 404


# ── Submit + grading + pass threshold ────────────────────────────────────────


async def test_submit_all_correct_passes_and_completes_submission(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.6})
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"},  # both correct indices
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"

    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.is_passed is True
    assert attempt.score == 2
    assert attempt.max_score == 2
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED


async def test_submit_below_threshold_fails(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.6})
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "0", "answer_1": "0"},  # both wrong
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.is_passed is False
    assert attempt.score == 0


async def test_submit_last_attempt_failure_marks_submission_failed(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.6, "max_quiz_attempts": 1}
    )
    await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "0", "answer_1": "0"},
        follow_redirects=False,
    )
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.FAILED


async def test_submit_on_terminal_attempt_redirects_to_result(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=False
    )
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit", data={}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"
    # No answers were written.
    answers = (
        (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    assert answers == []


# ── Timeout ──────────────────────────────────────────────────────────────────


async def test_show_quiz_times_out_when_over_limit(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    # 1-minute limit, started 2 minutes ago → timed out.
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "time_limit_minutes": 1},
        started_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    resp = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.TIMED_OUT


# ── Violations ───────────────────────────────────────────────────────────────


async def test_violation_warn_increments_count(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {
        "rules": [
            {"event": "tab_switch", "threshold": 1, "action": {"type": "warn", "message": "stop"}}
        ]
    }
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "warn"
    assert body["violation_count"] == 1


async def test_violation_fail_forces_force_fail_flag(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {
        "rules": [
            {"event": "tab_switch", "threshold": 1, "action": {"type": "fail", "message": "out"}}
        ]
    }
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "fail"
    await db.refresh(attempt)
    assert attempt.violations.get("_force_fail") is True


async def test_force_fail_attempt_finalizes_as_violation_fail(
    student_client: AsyncClient, db, student_user
) -> None:
    """An IN_PROGRESS attempt flagged _force_fail finalizes to VIOLATION_FAIL on view."""
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6},
        violations={"_force_fail": True},
    )
    resp = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.VIOLATION_FAIL
    assert attempt.is_passed is False


async def test_violation_event_ignored_on_terminal_attempt(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=True
    )
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"action": "none", "violation_count": 0}


# ── Result visibility ────────────────────────────────────────────────────────


async def test_result_visible_to_owning_student(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, is_passed=True
    )
    attempt.score = 2
    attempt.max_score = 2
    await db.commit()
    resp = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert resp.status_code == 200


async def test_result_of_in_progress_attempt_400(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)  # IN_PROGRESS
    resp = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert resp.status_code == 400


# ── Snapshot upload gating ───────────────────────────────────────────────────


async def test_snapshot_other_students_attempt_403(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    _subject, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/snapshot?event_type=face_lost",
        files={"frame": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert resp.status_code == 403


async def test_snapshot_rejected_when_capture_disabled(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _subject, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    # config_snapshot has no anti_cheat.camera.capture_snapshots → 403.
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={})
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/snapshot?event_type=face_lost",
        files={"frame": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert resp.status_code == 403
