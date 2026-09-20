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
        subject_id=subject.id,
        title="L1",
        code="l1",
        max_grade=100,
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
    # The finalizer moves it to FAILED (covered further down); emulate via the state machine.
    from submissions_checker.core.state_machine import transition

    transition(sub, "quiz_failed")
    await db.commit()
    assert await quiz_grants.grant_extra_attempt(db, sub, student.id) == 2


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
    r = await client.post(
        f"/teacher/submissions/{sub.id}/grant-quiz-attempt", follow_redirects=False
    )
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
