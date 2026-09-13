"""Functional coverage for check-free review modes and the per-question stepper.

Drives the real handlers over ASGI against a Postgres testcontainer. Two things are being
proved here: a submission can reach — and clear — a quiz without any sandbox ever running,
and a per-question-timed quiz is stepped one question at a time with the server, not the
browser, deciding when a question has expired.
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


def _question(text: str, correct: int, *, seconds: int | None = None) -> dict:
    q = {"type": "single_choice", "text": text, "points": 1,
         "options": ["wrong", "right"], "correct": correct}
    if seconds is not None:
        q["time_limit_seconds"] = seconds
    return q


# Three questions, each on its own 30-second clock. No shuffling, so question ids are their
# config indices and option 1 is always the correct one.
STEPPED_QUIZ = {
    "questions": [
        _question("q1", 1, seconds=30),
        _question("q2", 1, seconds=30),
        _question("q3", 1, seconds=30),
    ],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.6,
    "show_correct_answers_after": True,
}

FLAT_QUIZ = {
    "questions": [_question("q1", 1), _question("q2", 1)],
    "shuffle_questions": False,
    "shuffle_options": False,
    "pass_threshold_pct": 0.6,
}


async def _arrange(
    db,
    student_id: int,
    owner_id: int,
    *,
    quiz_cfg: dict,
    review_mode: str = "quiz_then_teacher",
    max_grade: int = 8,
) -> tuple[Subject, StudentAssignment, Submission]:
    """A QUIZ_SENT submission on an assignment that declares no checker at all."""
    subject = Subject(name="WinAPI", owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))

    assignment_cfg = {
        "review_mode": review_mode,
        "grading": {"code_weight": 0, "quiz_weight": 1},
        "quiz": quiz_cfg,
    }
    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-{subject.id}",
        config={"assignments": {"lab1": assignment_cfg}},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    sub_a = SubjectsAssignment(
        subject_id=subject.id, title="Lab 1", code="lab1",
        config={"review_mode": review_mode, "grading": {"code_weight": 0, "quiz_weight": 1}},
        min_grade=0, max_grade=max_grade,
    )
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
        source_metadata={"saved_as": "report.zip"},
        status=SubmissionStatus.QUIZ_SENT,
        test_results={"skipped": True, "reason": review_mode},
        plugin_config_id=cfg.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()

    return subject, sa, submission


async def _start(client: AsyncClient, subject: Subject, sa: StudentAssignment) -> None:
    resp = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


async def _attempt_of(db, submission_id: int) -> QuizAttempt:
    result = await db.execute(
        select(QuizAttempt).where(QuizAttempt.submission_id == submission_id)
    )
    return result.scalars().one()


# ── stepper delivery ─────────────────────────────────────────────────────────


async def test_stepped_quiz_serves_one_question_at_a_time(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    assert attempt.config_snapshot["per_question_timing"] is True
    assert attempt.question_started_at is not None
    assert attempt.current_index == 0

    page = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert page.status_code == 200
    # Only the current question is on the page.
    assert "q1" in page.text
    assert "q2" not in page.text
    assert 'name="index"' in page.text


async def test_answering_advances_and_finalizes_on_the_last_question(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    for i in range(3):
        resp = await student_client.post(
            f"/portal/quiz/{attempt.id}/answer",
            data={"index": str(i), f"answer_{i}": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}/result"

    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.score == 3
    assert attempt.max_score == 3
    assert attempt.is_passed is True


async def test_a_stale_index_changes_nothing(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    # Answer question 0 normally.
    await student_client.post(
        f"/portal/quiz/{attempt.id}/answer",
        data={"index": "0", "answer_0": "1"}, follow_redirects=False,
    )
    # A stale tab replays the same index — must not record a second answer or advance.
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/answer",
        data={"index": "0", "answer_0": "0"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}"

    await db.refresh(attempt)
    assert attempt.current_index == 1
    answers = (await db.execute(
        select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)
    )).scalars().all()
    assert len(answers) == 1
    assert answers[0].is_correct is True


async def test_expired_question_is_recorded_and_surfaced_on_the_result_page(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    # Wind the clock back past the first question's 30-second window.
    attempt.question_started_at = datetime.now(UTC) - timedelta(seconds=45)
    await db.commit()

    page = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert page.status_code == 200
    assert "q2" in page.text  # advanced past the expired q1

    await db.refresh(attempt)
    assert attempt.current_index == 1
    burned = (await db.execute(
        select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)
    )).scalars().one()
    assert burned.timed_out is True
    assert burned.points_earned == 0

    # Finish the remaining two correctly, then check the result page calls out the timeout.
    for i in (1, 2):
        await student_client.post(
            f"/portal/quiz/{attempt.id}/answer",
            data={"index": str(i), f"answer_{i}": "1"}, follow_redirects=False,
        )

    result = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert result.status_code == 200
    assert "Час вичерпано" in result.text

    await db.refresh(attempt)
    assert attempt.score == 2
    assert attempt.max_score == 3


async def test_an_answer_arriving_after_its_window_scores_zero(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    attempt.question_started_at = datetime.now(UTC) - timedelta(seconds=45)
    await db.commit()

    # The correct answer, posted too late.
    await student_client.post(
        f"/portal/quiz/{attempt.id}/answer",
        data={"index": "0", "answer_0": "1"}, follow_redirects=False,
    )

    answers = (await db.execute(
        select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)
    )).scalars().all()
    assert len(answers) == 1
    assert answers[0].timed_out is True
    assert answers[0].points_earned == 0


async def test_bulk_submit_is_refused_for_a_stepped_attempt(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=STEPPED_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1", "answer_2": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}"

    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS


# ── backwards compatibility ──────────────────────────────────────────────────


async def test_quiz_without_per_question_limits_still_renders_one_page(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=FLAT_QUIZ
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    assert "per_question_timing" not in attempt.config_snapshot
    assert attempt.question_started_at is None

    page = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert page.status_code == 200
    assert "q1" in page.text and "q2" in page.text  # all questions on one page

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.score == 2


# ── review-mode routing ──────────────────────────────────────────────────────


async def test_quiz_then_teacher_routes_a_pass_to_teacher_review(
    student_client: AsyncClient, db, student_user, teacher, login
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id,
        quiz_cfg=FLAT_QUIZ, review_mode="quiz_then_teacher", max_grade=8,
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)
    assert attempt.config_snapshot["review_mode"] == "quiz_then_teacher"

    await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"}, follow_redirects=False,
    )

    await db.refresh(submission)
    assert submission.status == SubmissionStatus.AWAITING_TEACHER_REVIEW

    # Approving must complete it, not bounce the student back into the quiz.
    # student_client and teacher_client are the same underlying client, so re-auth in place.
    login(student_client, teacher)
    resp = await student_client.post(
        f"/teacher/submissions/{submission.id}/review",
        data={"action": "approve", "reason": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(submission)
    assert submission.status == SubmissionStatus.COMPLETED

    # The grade was written by the app's session, so read it back off the database
    # rather than out of this session's identity map.
    grade = await db.scalar(select(StudentAssignment.grade).where(StudentAssignment.id == sa.id))
    assert grade == 8  # 100% of the quiz, scaled into the 0–8 band


async def test_quiz_only_completes_without_a_teacher(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await _arrange(
        db, student_user.student_id, teacher.id,
        quiz_cfg=FLAT_QUIZ, review_mode="quiz_only", max_grade=8,
    )
    await _start(student_client, subject, sa)
    attempt = await _attempt_of(db, submission.id)

    await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"}, follow_redirects=False,
    )

    await db.refresh(submission)
    assert submission.status == SubmissionStatus.COMPLETED
    grade = await db.scalar(select(StudentAssignment.grade).where(StudentAssignment.id == sa.id))
    assert grade == 8


# ── teacher gets at the attached work ────────────────────────────────────────


async def test_teacher_can_download_the_attached_work(
    student_client: AsyncClient, db, student_user, teacher, login, tmp_path, monkeypatch
) -> None:
    from submissions_checker.api.routes import teacher_portal

    monkeypatch.setattr(teacher_portal, "UPLOADS_DIR", tmp_path)
    (tmp_path / "report.zip").write_bytes(b"PK\x03\x04 pretend archive")

    _subject, _sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=FLAT_QUIZ
    )
    submission.status = SubmissionStatus.AWAITING_TEACHER_REVIEW
    submission.source_metadata = {"saved_as": "report.zip", "original_filename": "lab1.zip"}
    await db.commit()

    login(student_client, teacher)
    resp = await student_client.get(f"/teacher/submissions/{submission.id}/download")
    assert resp.status_code == 200
    assert resp.content == b"PK\x03\x04 pretend archive"
    assert "lab1.zip" in resp.headers["content-disposition"]


async def test_download_is_refused_to_another_teacher(
    student_client: AsyncClient, db, student_user, teacher, make_user, login, tmp_path, monkeypatch
) -> None:
    from submissions_checker.api.routes import teacher_portal

    monkeypatch.setattr(teacher_portal, "UPLOADS_DIR", tmp_path)
    (tmp_path / "report.zip").write_bytes(b"PK\x03\x04 pretend archive")

    _subject, _sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=FLAT_QUIZ
    )
    submission.source_metadata = {"saved_as": "report.zip", "original_filename": "lab1.zip"}
    await db.commit()

    from submissions_checker.db.models.enums import UserRole

    other = await make_user(role=UserRole.TEACHER, username="other_teacher")
    login(student_client, other)
    resp = await student_client.get(f"/teacher/submissions/{submission.id}/download")
    assert resp.status_code == 403


async def test_download_404s_when_the_file_is_gone(
    student_client: AsyncClient, db, student_user, teacher, login, tmp_path, monkeypatch
) -> None:
    from submissions_checker.api.routes import teacher_portal

    monkeypatch.setattr(teacher_portal, "UPLOADS_DIR", tmp_path)

    _subject, _sa, submission = await _arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=FLAT_QUIZ
    )
    submission.source_metadata = {"saved_as": "vanished.zip"}
    await db.commit()

    login(student_client, teacher)
    resp = await student_client.get(f"/teacher/submissions/{submission.id}/download")
    assert resp.status_code == 404
