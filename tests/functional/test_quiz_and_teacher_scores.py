"""quiz_and_teacher_scores: the quiz opens without an upload, the teacher enters points.

Service-level tests first (real Postgres, no HTTP), then the student and teacher routes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    AuditLog,
    OutboxMessage,
    QuizAttempt,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import (
    OutboxEventType,
    QuizAttemptStatus,
    SubmissionSourceType,
    SubmissionStatus,
    UserRole,
)
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import quiz_regrade
from submissions_checker.services.grading import finalize_grade
from tests.functional.conftest import authenticate

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


# ── Student opens the quiz without an upload ─────────────────────────────────


async def _as_student(client, db, make_user, student):
    user = await make_user(role=UserRole.STUDENT, username=f"s{student.id}", student=student)
    authenticate(client, user)
    return user


async def _subs_of(db, sa) -> list[Submission]:
    sa_id = sa.id  # read before expire_all, or the access lazy-loads outside the loop
    db.expire_all()
    rows = await db.execute(select(Submission).where(Submission.students_assignment_id == sa_id))
    return list(rows.scalars().all())


def _quiz_url(subject, sa) -> str:
    return f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz"


async def _open_and_answer(client, db, subject, sa, answer: str) -> QuizAttempt:
    r = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    assert r.status_code == 303, r.text
    attempt_id = int(r.headers["location"].rsplit("/", 1)[-1])
    attempt = await db.get(QuizAttempt, attempt_id)
    form = {f"answer_{q['id']}": answer for q in attempt.questions_snapshot}
    r = await client.post(f"/portal/quiz/{attempt_id}/submit", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return attempt


async def test_student_opens_quiz_without_upload(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    await _as_student(client, db, make_user, student)
    r = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/portal/quiz/")
    subs = await _subs_of(db, sa)
    assert len(subs) == 1
    assert subs[0].source_type == SubmissionSourceType.QUIZ_ONLY
    assert subs[0].status == SubmissionStatus.QUIZ_SENT
    assert subs[0].plugin_config_id is not None


async def test_second_open_reuses_the_submission(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    await _as_student(client, db, make_user, student)
    first = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    second = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    assert first.headers["location"] == second.headers["location"]  # resumed, not redrawn
    assert len(await _subs_of(db, sa)) == 1


async def test_open_refused_for_other_modes(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(
        db, teacher, make_student, mode="quiz_then_teacher"
    )
    await _as_student(client, db, make_user, student)
    r = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    assert r.status_code == 403
    assert await _subs_of(db, sa) == []


async def test_pass_without_points_goes_to_teacher(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    await _as_student(client, db, make_user, student)
    await _open_and_answer(client, db, subject, sa, "1")
    (sub,) = await _subs_of(db, sa)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    await db.refresh(sa)
    assert sa.grade is None


async def test_pass_with_points_completes(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(
        db, teacher, make_student, scores={"report": 5}
    )
    await _as_student(client, db, make_user, student)
    await _open_and_answer(client, db, subject, sa, "1")
    (sub,) = await _subs_of(db, sa)
    assert sub.status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 13  # 8 (100% quiz) + report 5


async def test_fail_with_attempts_left_stays_open(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    await _as_student(client, db, make_user, student)
    await _open_and_answer(client, db, subject, sa, "0")
    (sub,) = await _subs_of(db, sa)
    assert sub.status == SubmissionStatus.QUIZ_SENT


async def test_legacy_quiz_then_teacher_attempt_routes_by_current_mode(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa, sub = await _arrange_scored(
        db,
        teacher,
        make_student,
        scores={"report": 5},
        source_type=SubmissionSourceType.ZIP_UPLOAD,
    )
    await _as_student(client, db, make_user, student)
    r = await client.get(_quiz_url(subject, sa), follow_redirects=False)
    attempt_id = int(r.headers["location"].rsplit("/", 1)[-1])
    attempt = await db.get(QuizAttempt, attempt_id)
    # As if the attempt had been started under the old quiz_then_teacher config.
    attempt.config_snapshot = {**attempt.config_snapshot, "review_mode": "quiz_then_teacher"}
    await db.commit()
    form = {f"answer_{q['id']}": "1" for q in attempt.questions_snapshot}
    r = await client.post(f"/portal/quiz/{attempt_id}/submit", data=form, follow_redirects=False)
    assert r.status_code == 303
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 13


async def test_upload_refused_in_scored_mode(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, _asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    await _as_student(client, db, make_user, student)
    files = {"file": ("w.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")}
    r = await client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files=files,
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert await _subs_of(db, sa) == []


async def test_dispute_regrade_completes_when_points_are_in(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.FAILED, scores={"report": 3}
    )
    attempt = await _attempt(db, sub, sa.student_id, is_passed=False)
    attempt.is_passed = True
    attempt.score = 1
    await db.flush()
    await db.refresh(attempt, attribute_names=["submission"])
    await quiz_regrade._advance_submission(db, attempt)
    await db.commit()
    await db.refresh(sub)
    await db.refresh(sa)
    assert sub.status == SubmissionStatus.COMPLETED
    assert sa.grade == 11  # 8 + 3


async def test_dispute_regrade_waits_for_teacher_without_points(db, teacher, make_student) -> None:
    *_, sa, sub = await _arrange_scored(db, teacher, make_student, status=SubmissionStatus.FAILED)
    attempt = await _attempt(db, sub, sa.student_id, is_passed=False)
    attempt.is_passed = True
    attempt.score = 1
    await db.flush()
    await db.refresh(attempt, attribute_names=["submission"])
    await quiz_regrade._advance_submission(db, attempt)
    await db.commit()
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW


# ── Teacher enters points on the board ───────────────────────────────────────


def _scores_url(subject, asg) -> str:
    return f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores"


def _form(student, **scores: str) -> dict[str, str]:
    return {"student_id": str(student.id), **{f"score_{k}": v for k, v in scores.items()}}


async def _reload(db, *objs) -> None:
    for o in objs:
        await db.refresh(o)


async def test_teacher_saves_points_before_quiz(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="4", star=""), follow_redirects=False
    )
    assert r.status_code == 303, r.text
    await _reload(db, sa)
    assert sa.teacher_scores == {"report": 4}
    assert sa.grade is None
    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "teacher_scores_set"))
    ).scalar_one()
    assert log.detail["new"] == {"report": 4}
    assert log.detail["student_id"] == student.id


async def test_saving_points_completes_waiting_submission(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    await _attempt(db, sub, student.id, is_passed=True, score=3, max_score=4)
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="4", star="2"), follow_redirects=False
    )
    assert r.status_code == 303, r.text
    await _reload(db, sa, sub)
    assert sub.status == SubmissionStatus.COMPLETED
    assert sa.grade == 12
    outbox = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.SUBMISSION_REVIEWED
            )
        )
    ).scalar_one()
    assert outbox.payload["submission_id"] == sub.id


async def test_legacy_zip_submission_completes_on_points(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, sa, sub = await _arrange_scored(
        db,
        teacher,
        make_student,
        status=SubmissionStatus.AWAITING_TEACHER_REVIEW,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
    )
    await _attempt(db, sub, student.id, is_passed=True, review_mode="quiz_then_teacher")
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="5"), follow_redirects=False
    )
    assert r.status_code == 303, r.text
    await _reload(db, sa, sub)
    assert sub.status == SubmissionStatus.COMPLETED
    assert sa.grade == 13


async def test_editing_points_after_completion_regrades(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.COMPLETED, scores={"report": 4}
    )
    await _attempt(db, sub, student.id, is_passed=True)
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="5"), follow_redirects=False
    )
    assert r.status_code == 303, r.text
    await _reload(db, sa, sub)
    assert sa.grade == 13
    assert sub.status == SubmissionStatus.COMPLETED


async def test_clearing_required_after_completion_refused(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, sa, _sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.COMPLETED, scores={"report": 4}
    )
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report=""), follow_redirects=False
    )
    assert r.status_code == 422
    await _reload(db, sa)
    assert sa.teacher_scores == {"report": 4}


@pytest.mark.parametrize("value", ["abc", "-1", "6"])
async def test_bad_value_rejected(
    client: AsyncClient, db, teacher, make_student, value: str
) -> None:
    subject, asg, student, sa = await _arrange_scored_no_sub(db, teacher, make_student)
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report=value), follow_redirects=False
    )
    assert r.status_code == 422
    await _reload(db, sa)
    assert sa.teacher_scores is None


async def test_scores_route_other_teacher_403(
    client: AsyncClient, db, teacher, make_user, make_student
) -> None:
    subject, asg, student, _sa = await _arrange_scored_no_sub(db, teacher, make_student)
    other = await make_user(role=UserRole.TEACHER, username="other")
    authenticate(client, other)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="4"), follow_redirects=False
    )
    assert r.status_code == 403


async def test_scores_route_not_scored_mode_409(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, _sa = await _arrange_scored_no_sub(
        db, teacher, make_student, mode="quiz_then_teacher"
    )
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(student, report="4"), follow_redirects=False
    )
    assert r.status_code == 409


async def test_scores_route_unenrolled_student_404(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, _student, _sa = await _arrange_scored_no_sub(db, teacher, make_student)
    stranger = await make_student(full_name="Not Enrolled")
    authenticate(client, teacher)
    r = await client.post(
        _scores_url(subject, asg), data=_form(stranger, report="4"), follow_redirects=False
    )
    assert r.status_code == 404


async def test_review_approve_refused_without_points(
    client: AsyncClient, db, teacher, make_student
) -> None:
    _s, _a, student, _sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    await _attempt(db, sub, student.id, is_passed=True)
    authenticate(client, teacher)
    r = await client.post(
        f"/teacher/submissions/{sub.id}/review", data={"action": "approve"}, follow_redirects=False
    )
    assert r.status_code == 409
    await _reload(db, sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW


async def test_bulk_approve_skips_without_points(
    client: AsyncClient, db, teacher, make_student
) -> None:
    subject, asg, student, _sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    await _attempt(db, sub, student.id, is_passed=True)
    authenticate(client, teacher)
    r = await client.post(
        f"/teacher/subjects/{subject.id}/assignments/{asg.id}/bulk",
        data={"action": "approve", "submission_ids": [str(sub.id)]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("?bulk=0,1")
    await _reload(db, sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW


async def test_rerun_refused_for_quiz_only(client: AsyncClient, db, teacher, make_student) -> None:
    _s, _a, _st, _sa, sub = await _arrange_scored(
        db, teacher, make_student, status=SubmissionStatus.FAILED
    )
    authenticate(client, teacher)
    r = await client.post(f"/teacher/submissions/{sub.id}/rerun-checks", follow_redirects=False)
    assert r.status_code == 409
    await _reload(db, sub)
    assert sub.status == SubmissionStatus.FAILED
