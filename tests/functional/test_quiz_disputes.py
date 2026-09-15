"""Functional coverage for reporting a broken quiz question.

A student who believes a question is wrong or unanswerable flags it with a button next
to the question. Filing must be non-blocking: it happens mid-attempt, over fetch(), and
must not disturb the answers, the stepper cursor or the clock. The report lands in the
subject owner's notification bell with a link straight to the resolution panel.

Behavior is asserted against the real handlers in ``api/routes/student_quiz.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    Notification,
    OutboxEventType,
    OutboxMessage,
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
from submissions_checker.db.models.enums import QuizAttemptStatus, QuizDisputeStatus, UserRole
from submissions_checker.db.models.quiz_dispute import QuizQuestionDispute, QuizQuestionOverride
from submissions_checker.db.models.quiz_template import QuizAnswer
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig

pytestmark = pytest.mark.asyncio


# ── Arrangement ──────────────────────────────────────────────────────────────

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
}

SNAPSHOT = [
    {
        "id": 0,
        "type": "SINGLE_CHOICE",
        "text": "2+2",
        "points": 1,
        "is_required": False,
        "time_limit_seconds": None,
        "config": {"options": ["3", "4", "5"], "correct": 1},
    },
    {
        "id": 1,
        "type": "SINGLE_CHOICE",
        "text": "sky",
        "points": 1,
        "is_required": False,
        "time_limit_seconds": None,
        "config": {"options": ["green", "blue"], "correct": 1},
    },
]


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


async def _arrange_quiz(
    db,
    student_id: int,
    *,
    owner_id: int | None = None,
    quiz_cfg: dict | None = None,
    submission_status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
) -> tuple[Subject, StudentAssignment, Submission, SubjectPluginConfig]:
    """Build a fully-pinned submission ready for a quiz attempt."""
    subject = Subject(name=f"Quizland-{student_id}-{owner_id}", owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-{subject.id}",
        config={"assignments": {"hw1": {"quiz": quiz_cfg or QUIZ_CONFIG}}},
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
    status: QuizAttemptStatus = QuizAttemptStatus.IN_PROGRESS,
    config_snapshot: dict | None = None,
    questions_snapshot: list | None = None,
    started_at: datetime | None = None,
    question_started_at: datetime | None = None,
    current_index: int = 0,
    score: int | None = None,
    max_score: int | None = None,
    is_passed: bool | None = None,
) -> QuizAttempt:
    attempt = QuizAttempt(
        submission_id=submission_id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=questions_snapshot if questions_snapshot is not None else SNAPSHOT,
        config_snapshot=config_snapshot or {"pass_threshold_pct": 0.6},
        started_at=started_at or datetime.now(UTC),
        question_started_at=question_started_at,
        current_index=current_index,
        status=status,
        violations={},
        score=score,
        max_score=max_score,
        is_passed=is_passed,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


# ── Filing a dispute ─────────────────────────────────────────────────────────


async def test_dispute_creates_open_row(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute",
        json={"question_id": 1, "note": "both answers are defensible"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    row = (await db.execute(select(QuizQuestionDispute))).scalar_one()
    assert row.id == body["dispute_id"]
    assert row.attempt_id == attempt.id
    assert row.question_id == 1
    assert row.student_id == student_user.student_id
    assert row.student_note == "both answers are defensible"
    assert row.status == QuizDisputeStatus.OPEN
    assert row.teacher_note is None
    assert row.resolved_at is None


async def test_dispute_notifies_the_subject_owner_with_a_panel_link(
    student_client: AsyncClient, db, student_user, make_user
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": ""}
    )
    assert resp.status_code == 200
    dispute_id = resp.json()["dispute_id"]

    notes = (await db.execute(select(Notification))).scalars().all()
    assert [n.user_id for n in notes] == [owner.id]
    assert notes[0].link == f"/teacher/disputes/{dispute_id}"
    assert notes[0].is_read is False


async def test_dispute_falls_back_to_admins_when_subject_has_no_owner(
    student_client: AsyncClient, db, student_user, make_user
) -> None:
    admin = await make_user(role=UserRole.ADMIN, username="admin1")
    await make_user(role=UserRole.ADMIN, username="admin2", is_active=False)
    await make_user(role=UserRole.TEACHER, username="unrelated-teacher")
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=None)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0})
    assert resp.status_code == 200

    notes = (await db.execute(select(Notification))).scalars().all()
    # The inactive admin and the unrelated teacher are not notified.
    assert [n.user_id for n in notes] == [admin.id]


async def test_dispute_works_after_the_attempt_is_submitted(
    student_client: AsyncClient, db, student_user
) -> None:
    """The results page carries the same button — an appeal is most likely filed there."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.COMPLETED,
        score=1,
        max_score=2,
        is_passed=False,
    )

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": "key is wrong"}
    )
    assert resp.status_code == 200
    row = (await db.execute(select(QuizQuestionDispute))).scalar_one()
    assert row.question_id == 0


async def test_disputes_are_unlimited(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)

    first = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": "one"}
    )
    second = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": "two"}
    )
    assert first.status_code == 200
    assert second.status_code == 200

    rows = (await db.execute(select(QuizQuestionDispute))).scalars().all()
    assert sorted(r.student_note for r in rows) == ["one", "two"]


# ── Filing must not disturb the attempt ──────────────────────────────────────


async def test_dispute_leaves_answers_cursor_and_clock_untouched(
    student_client: AsyncClient, db, student_user
) -> None:
    """Non-blocking by design: flagging a question costs the student nothing."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    started = datetime.now(UTC) - timedelta(seconds=30)
    question_started = datetime.now(UTC) - timedelta(seconds=10)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"per_question_timing": True, "time_limit_minutes": 10},
        started_at=started,
        question_started_at=question_started,
        current_index=1,
    )
    db.add(
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=0,
            answer={"selected": 1},
            is_correct=True,
            points_earned=1,
        )
    )
    await db.commit()

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 1, "note": "unclear"}
    )
    assert resp.status_code == 200

    await db.refresh(attempt)
    assert attempt.current_index == 1
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS
    assert attempt.started_at.replace(tzinfo=UTC) == started
    assert attempt.question_started_at.replace(tzinfo=UTC) == question_started
    answers = (
        (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    assert [(a.question_id, a.points_earned) for a in answers] == [(0, 1)]


# ── Rejections ───────────────────────────────────────────────────────────────


async def test_dispute_on_another_students_attempt_is_forbidden(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    """403 rather than 404, matching the ownership check every other quiz endpoint uses."""
    await _consent(db, student_user.student_id)
    other = await make_student()
    _s, _sa, sub, cfg = await _arrange_quiz(db, other.id)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0})
    assert resp.status_code == 403
    assert (await db.execute(select(QuizQuestionDispute))).first() is None


async def test_dispute_on_a_question_not_in_the_snapshot_is_rejected(
    student_client: AsyncClient, db, student_user
) -> None:
    """The snapshot is a random draw — a question the student never saw is not theirs to flag."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 99})
    assert resp.status_code == 400
    assert (await db.execute(select(QuizQuestionDispute))).first() is None


async def test_dispute_requires_a_question_id(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/dispute", json={"note": "x"})
    assert resp.status_code == 400


async def test_dispute_on_a_missing_attempt_is_not_found(
    student_client: AsyncClient, db, student_user
) -> None:
    resp = await student_client.post("/portal/quiz/999999/dispute", json={"question_id": 0})
    assert resp.status_code == 404


async def test_dispute_requires_a_logged_in_student(client: AsyncClient) -> None:
    resp = await client.post("/portal/quiz/1/dispute", json={"question_id": 0})
    assert resp.status_code in (401, 403)


async def test_teacher_cannot_file_a_dispute(teacher_client: AsyncClient, db, student_user) -> None:
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)
    resp = await teacher_client.post(f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0})
    assert resp.status_code == 403


# ── Teacher resolution panel ─────────────────────────────────────────────────


async def _file_dispute(
    db,
    attempt: QuizAttempt,
    student_id: int,
    question_id: int,
    *,
    note: str | None = "looks wrong",
) -> QuizQuestionDispute:
    dispute = QuizQuestionDispute(
        attempt_id=attempt.id,
        question_id=question_id,
        student_id=student_id,
        plugin_config_id=attempt.plugin_config_id,
        plugin_config_version=attempt.plugin_config_version,
        student_note=note,
        status=QuizDisputeStatus.OPEN,
    )
    db.add(dispute)
    await db.commit()
    await db.refresh(dispute)
    return dispute


async def _answer(db, attempt: QuizAttempt, question_id: int, *, correct: bool) -> QuizAnswer:
    points = next(q["points"] for q in attempt.questions_snapshot if q["id"] == question_id)
    answer = QuizAnswer(
        attempt_id=attempt.id,
        question_id=question_id,
        answer={"selected": 1 if correct else 0},
        is_correct=correct,
        points_earned=points if correct else 0,
    )
    db.add(answer)
    await db.commit()
    return answer


async def test_panel_lists_open_disputes_for_owned_subjects(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, owner)
    resp = await client.get("/teacher/disputes")
    assert resp.status_code == 200
    assert "2+2" in resp.text


async def test_panel_hides_disputes_from_another_teachers_subject(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    stranger = await make_user(role=UserRole.TEACHER, username="stranger")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, stranger)
    resp = await client.get("/teacher/disputes")
    assert resp.status_code == 200
    assert "2+2" not in resp.text


async def test_panel_detail_shows_the_key_as_this_student_saw_it(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    """Options are shuffled per attempt, so only the snapshot's key means anything."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    shuffled = [
        {
            "id": 0,
            "type": "SINGLE_CHOICE",
            "text": "2+2",
            "points": 1,
            "is_required": False,
            "time_limit_seconds": None,
            # "4" moved to index 2 for this student; the config says index 1.
            "config": {"options": ["3", "5", "4"], "correct": 2},
        }
    ]
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, questions_snapshot=shuffled
    )
    await _answer(db, attempt, 0, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0, note="ambiguous")

    login(client, owner)
    resp = await client.get(f"/teacher/disputes/{dispute.id}")
    assert resp.status_code == 200
    assert "ambiguous" in resp.text
    # The correct option is the one the snapshot points at, not the config's raw index.
    assert resp.text.index("4") < len(resp.text)
    assert 'data-correct-option="2"' in resp.text


async def test_panel_detail_is_forbidden_for_another_teacher(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    stranger = await make_user(role=UserRole.TEACHER, username="stranger")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, stranger)
    resp = await client.get(f"/teacher/disputes/{dispute.id}")
    assert resp.status_code == 403


async def test_students_cannot_reach_the_panel(
    student_client: AsyncClient, db, student_user
) -> None:
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0)
    assert (await student_client.get("/teacher/disputes")).status_code == 403
    assert (await student_client.get(f"/teacher/disputes/{dispute.id}")).status_code == 403


async def test_resolution_requires_a_note(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    """The note is the point of the panel — a ruling with no reason is not a ruling."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 400

    await db.refresh(dispute)
    assert dispute.status == QuizDisputeStatus.OPEN
    assert (await db.execute(select(QuizQuestionOverride))).first() is None


async def test_resolution_rejects_an_unknown_action(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "maybe", "note": "hmm"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_reject_records_the_note_and_changes_no_score(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, attempt, 0, correct=True)
    await _answer(db, attempt, 1, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "reject", "note": "the key is right, 'blue' is the answer"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(dispute)
    await db.refresh(attempt)
    assert dispute.status == QuizDisputeStatus.REJECTED
    assert dispute.teacher_note == "the key is right, 'blue' is the answer"
    assert dispute.resolved_by_user_id == owner.id
    assert dispute.resolved_at is not None
    assert (attempt.score, attempt.is_passed) == (1, False)
    assert (await db.execute(select(QuizQuestionOverride))).first() is None


async def test_already_resolved_dispute_cannot_be_resolved_again(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 0)

    login(client, owner)
    first = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "reject", "note": "fine"},
        follow_redirects=False,
    )
    second = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "changed my mind"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert second.status_code == 409


# ── Accept: credit the question for everyone who drew it ─────────────────────


async def test_accept_credits_the_question_and_records_an_override(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, attempt, 0, correct=True)
    wrong = await _answer(db, attempt, 1, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "both 'blue' and 'green' are defensible"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(dispute)
    assert dispute.status == QuizDisputeStatus.ACCEPTED
    assert dispute.teacher_note.startswith("both")

    override = (await db.execute(select(QuizQuestionOverride))).scalar_one()
    assert override.plugin_config_id == cfg.id
    assert override.plugin_config_version == cfg.version
    assert override.question_id == 1
    assert override.credit_all is True
    assert override.dispute_id == dispute.id
    assert override.created_by_user_id == owner.id

    await db.refresh(wrong)
    await db.refresh(attempt)
    assert wrong.is_correct is True
    assert wrong.points_earned == 1
    # max_score is unchanged; only the earned score rises.
    assert (attempt.score, attempt.max_score, attempt.is_passed) == (2, 2, True)

    await db.refresh(sa)
    assert sa.grade is not None


async def test_accept_reaches_every_attempt_on_the_same_config_version(
    client: AsyncClient, db, student_user, make_user, make_student, login
) -> None:
    """The headline behaviour: everyone trapped in the question is credited, not just the
    student who spoke up — and nobody outside that config version is touched."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)

    reporter = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, reporter, 0, correct=True)
    await _answer(db, reporter, 1, correct=False)

    # A silent classmate on the same config version who also got it wrong.
    quiet_student = await make_student()
    _s2, _sa2, sub2, _cfg2 = await _arrange_quiz(db, quiet_student.id, owner_id=owner.id)
    quiet = await _make_attempt(
        db, sub2.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, quiet, 0, correct=True)
    await _answer(db, quiet, 1, correct=False)

    # A classmate whose draw never included question 1.
    other_student = await make_student()
    _s3, _sa3, sub3, _cfg3 = await _arrange_quiz(db, other_student.id, owner_id=owner.id)
    undrawn = await _make_attempt(
        db,
        sub3.id,
        cfg,
        status=QuizAttemptStatus.COMPLETED,
        questions_snapshot=[SNAPSHOT[0]],
        score=0,
        max_score=1,
        is_passed=False,
    )
    await _answer(db, undrawn, 0, correct=False)

    dispute = await _file_dispute(db, reporter, student_user.student_id, 1)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(reporter)
    await db.refresh(quiet)
    await db.refresh(undrawn)
    assert (reporter.score, reporter.is_passed) == (2, True)
    assert (quiet.score, quiet.is_passed) == (2, True)
    # Never drew the question: untouched.
    assert (undrawn.score, undrawn.max_score, undrawn.is_passed) == (0, 1, False)


async def test_accept_skips_a_different_config_version(
    client: AsyncClient, db, student_user, make_user, make_student, login
) -> None:
    """A re-uploaded config may have fixed the question; its index means something else."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    reporter = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, reporter, 0, correct=True)
    await _answer(db, reporter, 1, correct=False)

    other = await make_student()
    _s2, _sa2, sub2, cfg_v2 = await _arrange_quiz(db, other.id, owner_id=owner.id)
    cfg_v2.version = 2
    await db.commit()
    later = await _make_attempt(
        db,
        sub2.id,
        cfg_v2,
        status=QuizAttemptStatus.COMPLETED,
        score=1,
        max_score=2,
        is_passed=False,
    )
    await _answer(db, later, 0, correct=True)
    await _answer(db, later, 1, correct=False)

    dispute = await _file_dispute(db, reporter, student_user.student_id, 1)
    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    await db.refresh(reporter)
    await db.refresh(later)
    assert reporter.score == 2
    assert (later.score, later.is_passed) == (1, False)


async def test_accept_creates_a_row_for_a_question_the_student_never_answered(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.TIMED_OUT, score=1, max_score=2, is_passed=False
    )
    await _answer(db, attempt, 0, correct=True)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "unanswerable"},
        follow_redirects=False,
    )

    rows = (
        (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    created = next(r for r in rows if r.question_id == 1)
    assert created.is_correct is True
    assert created.points_earned == 1
    await db.refresh(attempt)
    assert (attempt.score, attempt.is_passed) == (2, True)
    # Status is history and stays put: the attempt really did run out of time.
    assert attempt.status == QuizAttemptStatus.TIMED_OUT


async def test_accept_leaves_a_violation_fail_attempt_failed(
    client: AsyncClient, db, student_user, make_user, make_student, login
) -> None:
    """That attempt failed for cheating, which is not what the dispute was about."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    reporter = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, reporter, 0, correct=True)
    await _answer(db, reporter, 1, correct=False)

    cheat_student = await make_student()
    _s2, _sa2, sub2, _c = await _arrange_quiz(db, cheat_student.id, owner_id=owner.id)
    cheater = await _make_attempt(
        db,
        sub2.id,
        cfg,
        status=QuizAttemptStatus.VIOLATION_FAIL,
        score=1,
        max_score=2,
        is_passed=False,
    )
    await _answer(db, cheater, 0, correct=True)
    await _answer(db, cheater, 1, correct=False)

    dispute = await _file_dispute(db, reporter, student_user.student_id, 1)
    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    await db.refresh(cheater)
    assert (cheater.score, cheater.is_passed) == (1, False)
    assert cheater.status == QuizAttemptStatus.VIOLATION_FAIL


async def test_accept_resurrects_a_failed_submission(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    """The terminal-state gap: FAILED had no way out until a dispute was accepted."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, sa, sub, cfg = await _arrange_quiz(
        db,
        student_user.student_id,
        owner_id=owner.id,
        submission_status=SubmissionStatus.FAILED,
    )
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, attempt, 0, correct=True)
    await _answer(db, attempt, 1, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "the key was wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(attempt)
    await db.refresh(sub)
    await db.refresh(sa)
    assert attempt.is_passed is True
    assert sub.status == SubmissionStatus.COMPLETED
    assert sa.grade is not None


async def test_accept_routes_quiz_then_teacher_back_to_review(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(
        db,
        student_user.student_id,
        owner_id=owner.id,
        submission_status=SubmissionStatus.FAILED,
    )
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.COMPLETED,
        config_snapshot={"pass_threshold_pct": 0.6, "review_mode": "quiz_then_teacher"},
        score=1,
        max_score=2,
        is_passed=False,
    )
    await _answer(db, attempt, 0, correct=True)
    await _answer(db, attempt, 1, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    await db.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW


async def test_accept_resolves_sibling_disputes_on_the_same_question(
    client: AsyncClient, db, student_user, make_user, make_student, login
) -> None:
    """Disputes are unlimited, so one ruling has to close every open report on it."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    first = await _file_dispute(db, attempt, student_user.student_id, 1, note="a")
    duplicate = await _file_dispute(db, attempt, student_user.student_id, 1, note="b")
    unrelated = await _file_dispute(db, attempt, student_user.student_id, 0, note="c")

    other = await make_student()
    _s2, _sa2, sub2, _c = await _arrange_quiz(db, other.id, owner_id=owner.id)
    attempt2 = await _make_attempt(db, sub2.id, cfg, status=QuizAttemptStatus.COMPLETED)
    classmate = await _file_dispute(db, attempt2, other.id, 1, note="d")

    login(client, owner)
    await client.post(
        f"/teacher/disputes/{first.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    for d in (first, duplicate, classmate):
        await db.refresh(d)
        assert d.status == QuizDisputeStatus.ACCEPTED
        assert d.teacher_note == "bad question"
    await db.refresh(unrelated)
    assert unrelated.status == QuizDisputeStatus.OPEN


async def test_accept_notifies_every_affected_student(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)
    attempt = await _make_attempt(
        db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED, score=1, max_score=2, is_passed=False
    )
    await _answer(db, attempt, 0, correct=True)
    await _answer(db, attempt, 1, correct=False)
    dispute = await _file_dispute(db, attempt, student_user.student_id, 1)

    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    notes = (
        (await db.execute(select(Notification).where(Notification.user_id == student_user.id)))
        .scalars()
        .all()
    )
    assert len(notes) == 1
    assert notes[0].link == f"/portal/quiz/{attempt.id}/result"

    events = (
        (
            await db.execute(
                select(OutboxMessage).where(
                    OutboxMessage.event_type == OutboxEventType.QUIZ_DISPUTE_RESOLVED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["attempt_id"] == attempt.id
    assert events[0].payload["decision"] == "accept"
    assert events[0].payload["is_passed"] is True


async def test_an_in_progress_attempt_picks_up_the_override_at_submit(
    client: AsyncClient, db, student_user, make_user, login
) -> None:
    """The double-credit guard: a live attempt is credited once, at finalize, not twice."""
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id, owner_id=owner.id)

    reporter = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    dispute = await _file_dispute(db, reporter, student_user.student_id, 1)
    login(client, owner)
    await client.post(
        f"/teacher/disputes/{dispute.id}/resolve",
        data={"action": "accept", "note": "bad question"},
        follow_redirects=False,
    )

    live = await _make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.IN_PROGRESS)
    # Answers question 1 WRONGLY, then submits.
    login(client, student_user)
    resp = await client.post(
        f"/portal/quiz/{live.id}/submit",
        data={"answer_0": "1", "answer_1": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(live)
    rows = (
        (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == live.id)))
        .scalars()
        .all()
    )
    # Exactly one row for the credited question — not two.
    assert len([r for r in rows if r.question_id == 1]) == 1
    assert (live.score, live.max_score, live.is_passed) == (2, 2, True)
