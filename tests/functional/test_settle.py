"""settle(): the one place a student's assignment gets its grade and status."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    SubjectsAssignment,
    Submission,
)
from submissions_checker.db.models.enums import (
    QuizAttemptStatus,
    SubmissionSourceType,
    SubmissionStatus,
    UserRole,
)
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.services import settle
from tests.functional._scored_helpers import (
    CFG,
    arrange,
    attempt,
    squad_of_two,
    start_and_answer_correctly,
    submissions_of,
)
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


async def test_quiz_passed_waits_for_teacher(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert res.evaluation.first_pending().key == "report"
    await db.refresh(sa)
    assert sa.grade is None


async def test_scores_then_check_complete_in_any_order(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
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
    _, _, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    sub.source_metadata = {settle.REJECTED_KEY: {"reason": "copy"}}
    await attempt(db, sub, student.id, score=4, max_score=4, passed=True)
    assert (await settle.settle(db, sa)).new_status == SubmissionStatus.FAILED


async def test_completed_never_leaves_completed(db, teacher, make_student) -> None:
    _, _, student, sa, sub = await arrange(
        db,
        teacher,
        make_student,
        config=CFG,
        status=SubmissionStatus.COMPLETED,
        scores={"report": 4},
    )
    res = await settle.settle(db, sa)  # check missing: incomplete, but already graded
    assert res.new_status == SubmissionStatus.COMPLETED


async def test_teacher_only_plan_without_submission_creates_one(db, teacher, make_student) -> None:
    cfg = {"requirements": [{"type": "score", "key": "r", "title": "R", "max": 16}]}
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=cfg, with_submission=False, scores={"r": 11}
    )
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 11


async def test_incomplete_without_submission_writes_nothing(db, teacher, make_student) -> None:
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=CFG, with_submission=False, scores={"report": 4}
    )
    res = await settle.settle(db, sa)
    assert res.submission_id is None and res.new_status is None
    assert await submissions_of(db, sa) == []


async def test_squad_concurrent_settle_one_transition(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    sa_a, sa_b, sub = await squad_of_two(
        db,
        teacher,
        make_student,
        config=CFG,
        scores={"report": 5, "accepted": 1},
        both_passed=True,
    )

    async def run(sa_id: int) -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            row = await s.get(StudentAssignment, sa_id)
            res = await settle.settle(s, row)
            await s.commit()
            return res

    results = await asyncio.gather(run(sa_a.id), run(sa_b.id))
    assert [r.new_status for r in results] == [SubmissionStatus.COMPLETED] * 2
    assert sum(r.completed_now for r in results) == 1
    for sa_id in (sa_a.id, sa_b.id):
        async with functional_sessionmaker() as s:
            assert (await s.get(StudentAssignment, sa_id)).grade == 13  # ceil(8) + 5 + 0


async def test_squad_last_attempts_racing_still_complete(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    """A and B each add their own passing attempt and settle in their own transaction.
    A settles first and commits a moment later; B settles while A is uncommitted. B must
    wait for A and only then read the facts (seeing A's pass), or nobody completes."""
    sa_a, sa_b, sub = await squad_of_two(
        db, teacher, make_student, config=CFG, scores={"report": 5, "accepted": 1}
    )
    # Both attempts are inserted before either settles (an attempt insert takes a key-share
    # lock on the submission, so inserting after A locked it would serialise by accident).
    b_inserted = asyncio.Event()
    a_settled = asyncio.Event()

    async def member_a() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            await attempt(s, sub, sa_a.student_id, score=1, max_score=1, passed=True, commit=False)
            await b_inserted.wait()
            res = await settle.settle(s, await s.get(StudentAssignment, sa_a.id))
            a_settled.set()
            await asyncio.sleep(0.5)  # B is inside settle now, waiting on A's lock
            await s.commit()
            return res

    async def member_b() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            await attempt(s, sub, sa_b.student_id, score=1, max_score=1, passed=True, commit=False)
            b_inserted.set()
            await a_settled.wait()
            res = await settle.settle(s, await s.get(StudentAssignment, sa_b.id))
            await s.commit()
            return res

    res_a, res_b = await asyncio.wait_for(asyncio.gather(member_a(), member_b()), timeout=15)
    assert res_a.new_status == SubmissionStatus.QUIZ_SENT  # B's pass not committed yet
    assert res_b.completed_now
    async with functional_sessionmaker() as s:
        assert (await s.get(Submission, sub.id)).status == SubmissionStatus.COMPLETED
        for sa_id in (sa_a.id, sa_b.id):
            assert (await s.get(StudentAssignment, sa_id)).grade == 13


async def test_concurrent_settles_create_one_upload_less_submission(
    functional_sessionmaker, db, teacher, make_student
) -> None:
    cfg = {"requirements": [{"type": "score", "key": "r", "title": "R", "max": 16}]}
    _, _, _, sa = await arrange(
        db, teacher, make_student, config=cfg, with_submission=False, scores={"r": 11}
    )

    async def run() -> settle.SettleResult:
        async with functional_sessionmaker() as s:
            res = await settle.settle(s, await s.get(StudentAssignment, sa.id))
            await s.commit()
            return res

    results = await asyncio.gather(run(), run())
    assert sum(r.completed_now for r in results) == 1
    async with functional_sessionmaker() as s:
        n = await s.scalar(
            select(func.count(Submission.id)).where(Submission.students_assignment_id == sa.id)
        )
        assert n == 1


async def _queued(db, sub) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(TeacherNotificationQueue)
        .where(TeacherNotificationQueue.submission_id == sub.id)
    )


async def test_teacher_notified_only_once_the_work_waits_on_them(db, teacher, make_student) -> None:
    """A quiz still open (failed, attempts left) with teacher items owed parks the work with
    the teacher but does not ping them; the pass that leaves only their items does, once."""
    _, _, student, sa, sub = await arrange(db, teacher, make_student, config=CFG)
    await attempt(db, sub, student.id, score=0, max_score=4, passed=False)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert await _queued(db, sub) == 0
    await attempt(db, sub, student.id, score=4, max_score=4, passed=True)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AWAITING_TEACHER_REVIEW  # unchanged status
    assert await _queued(db, sub) == 1
    await settle.settle(db, sa, previous_status=SubmissionStatus.TESTING)
    assert await _queued(db, sub) == 1  # idempotent per submission


async def test_grant_route_settles_a_squad_member(db, client, teacher, make_student) -> None:
    """Granting the exhausted member of a squad takes the scope lock first, grants and
    settles: with the teacher's items in, only the quiz is left -> QUIZ_SENT."""
    sa_a, sa_b, sub = await squad_of_two(
        db,
        teacher,
        make_student,
        config=CFG,
        scores={"report": 4, "accepted": 1},
        status=SubmissionStatus.FAILED,
    )
    await attempt(db, sub, sa_a.student_id, score=1, max_score=1, passed=True)
    for _ in range(2):
        await attempt(db, sub, sa_b.student_id, score=0, max_score=1, passed=False)
    authenticate(client, teacher)
    r = await client.post(
        f"/teacher/submissions/{sub.id}/grant-quiz-attempt",
        data={"student_id": str(sa_b.student_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.QUIZ_SENT
    assert sub.source_metadata["quiz_extra_attempts"] == {str(sa_b.student_id): 1}


async def test_quiz_starts_on_rejected_older_version(
    db, client, teacher, make_user, make_student
) -> None:
    """A rejected first version keeps the attempts; a newer version is in. The quiz item is
    still pending, so the quiz starts — on the submission the attempts hang off."""
    subject, asg, student, sa, sub1 = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.FAILED
    )
    sub1.source_metadata = {settle.REJECTED_KEY: {"reason": "copy"}}
    await db.commit()
    await attempt(db, sub1, student.id, score=0, max_score=1, passed=False)
    sub2 = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=SubmissionStatus.AWAITING_TEACHER_REVIEW,
        plugin_config_id=sub1.plugin_config_id,
        test_results={"skipped": True, "reason": "requirements"},
    )
    db.add(sub2)
    await db.commit()
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303 and "/portal/quiz/" in r.headers["location"], r.text
    new = await db.get(QuizAttempt, int(r.headers["location"].rsplit("/", 1)[-1]))
    assert new.submission_id == sub1.id


async def test_in_progress_attempt_resumes_before_any_refusal(
    db, client, teacher, make_user, make_student
) -> None:
    subject, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.COMPLETED
    )
    running = await attempt(
        db,
        sub,
        student.id,
        score=0,
        max_score=1,
        passed=False,
        status=QuizAttemptStatus.IN_PROGRESS,
    )
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/portal/quiz/{running.id}"


async def test_quiz_before_upload_on_upload_source(db, client, teacher, make_user, make_student):
    """source: upload with tests + quiz: the quiz opens before any ZIP exists."""
    cfg = {
        "max_grade": 16,
        "requirements": [
            {"type": "tests", "key": "tests", "points": 8},
            {"type": "quiz", "key": "quiz", "points": 8},
        ],
    }
    subject, asg, student, sa = await arrange(
        db, teacher, make_student, config=cfg, with_submission=False
    )
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303 and "/portal/quiz/" in r.headers["location"], r.text


async def test_quiz_pass_runs_settle(db, client, teacher, make_user, make_student):
    """Passing the last required item over HTTP completes the work."""
    subject, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    attempt_id = await start_and_answer_correctly(client, subject.id, sa.id)
    r = await client.post(f"/portal/quiz/{attempt_id}/submit", follow_redirects=False)
    assert r.status_code in (200, 303)  # already terminal: straight to the result
    await db.refresh(sa)
    assert sa.grade == 12  # 8 + 4 + 0
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED
    # Once the quiz item is done it does not open again.
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 403


async def _completed_with_stale_grade(db, teacher, make_student):
    """COMPLETED work whose stored grade (99) no longer matches the current config's
    computation (8*0.75 + 4 + 0 = 10): what a config change after grading leaves behind."""
    _, _, student, sa, sub = await arrange(
        db,
        teacher,
        make_student,
        config=CFG,
        status=SubmissionStatus.COMPLETED,
        scores={"report": 4, "accepted": 1},
    )
    await attempt(db, sub, student.id, score=3, max_score=4, passed=True)
    sa.grade = 99
    sub.grade_breakdown = {"frozen": True}
    await db.commit()
    return sa, sub


async def test_completed_grade_is_fixed_by_default(db, teacher, make_student) -> None:
    """Spec §2: after COMPLETED the grade is fixed — an ordinary settle (a new Classroom
    version, a worker, a quiz) changes neither the grade nor the status."""
    sa, sub = await _completed_with_stale_grade(db, teacher, make_student)
    res = await settle.settle(db, sa)
    await db.commit()
    assert res.new_status == SubmissionStatus.COMPLETED and not res.completed_now
    await db.refresh(sa)
    await db.refresh(sub)
    assert sa.grade == 99
    assert sub.grade_breakdown == {"frozen": True}


async def test_regrade_completed_recomputes(db, teacher, make_student) -> None:
    sa, sub = await _completed_with_stale_grade(db, teacher, make_student)
    res = await settle.settle(db, sa, regrade_completed=True)
    await db.commit()
    assert res.new_status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 10


async def test_dispute_regrade_recomputes_a_completed_grade(db, teacher, make_student) -> None:
    from submissions_checker.services import quiz_regrade

    sa, sub = await _completed_with_stale_grade(db, teacher, make_student)
    a = (
        await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id))
    ).scalar_one()
    a.submission = sub
    await quiz_regrade._advance_submission(db, a)
    await db.commit()
    await db.refresh(sa)
    assert sa.grade == 10


async def _rejected_latest(db, teacher, make_student, make_user, client):
    subject, asg, student, sa, sub = await arrange(
        db, teacher, make_student, config=CFG, status=SubmissionStatus.FAILED
    )
    sub.source_metadata = {settle.REJECTED_KEY: {"reason": "Списано з <b>чужої</b> роботи"}}
    await db.commit()
    user = await make_user(role=UserRole.STUDENT, student=student)
    authenticate(client, user)
    return subject, sa


async def test_rejected_work_offers_no_quiz_and_says_why(
    db, client, teacher, make_user, make_student
) -> None:
    """A quiz passed on rejected work would leave it FAILED with no explanation: the page
    shows the rejection instead of the quiz button."""
    subject, sa = await _rejected_latest(db, teacher, make_student, make_user, client)
    r = await client.get(f"/portal/subjects/{subject.id}/assignments/{sa.id}")
    assert r.status_code == 200, r.text
    assert f"/assignments/{sa.id}/quiz" not in r.text
    assert (
        "Роботу відхилено: Списано з &lt;b&gt;чужої&lt;/b&gt; роботи. Здайте нову версію." in r.text
    )


async def test_rejected_work_refuses_quiz_start(
    db, client, teacher, make_user, make_student
) -> None:
    subject, sa = await _rejected_latest(db, teacher, make_student, make_user, client)
    r = await client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert r.status_code == 403


async def test_reject_refused_without_a_source(db, client, teacher, make_student) -> None:
    """No upload and no Classroom: a rejected student could never hand in a new version,
    so a rejection would be a dead end."""
    cfg = {
        "requirements": [
            {"type": "quiz", "key": "quiz", "points": 8},
            {"type": "score", "key": "report", "title": "Звіт", "max": 5},
        ]
    }
    *_, sub = await arrange(
        db, teacher, make_student, config=cfg, status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    authenticate(client, teacher)
    r = await client.post(
        f"/teacher/submissions/{sub.id}/review",
        data={"action": "reject", "reason": "копія"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert "new version" in r.json()["detail"]
    await db.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    assert settle.REJECTED_KEY not in (sub.source_metadata or {})


def _code_cfg(*, ai_optional: bool) -> dict:
    return {
        "source": "upload",
        "requirements": [
            {"type": "tests", "key": "tests", "points": 8},
            {"type": "ai_review", "key": "ai", "points": 2, "optional": ai_optional},
        ],
    }


async def _ai_failed(db, teacher, make_student, *, ai_optional: bool):
    _, _, _, sa, sub = await arrange(
        db,
        teacher,
        make_student,
        config=_code_cfg(ai_optional=ai_optional),
        status=SubmissionStatus.AI_REVIEW_FAILED,
    )
    sub.source_type = SubmissionSourceType.ZIP_UPLOAD
    sub.test_results = {"passed": True, "score": 2, "max_score": 2}
    await db.commit()
    return sa, sub


async def test_failed_optional_ai_review_does_not_block_completion(
    db, teacher, make_student
) -> None:
    sa, sub = await _ai_failed(db, teacher, make_student, ai_optional=True)
    res = await settle.settle(db, sa)
    await db.commit()
    assert res.new_status == SubmissionStatus.COMPLETED
    await db.refresh(sa)
    assert sa.grade == 8


async def test_failed_required_ai_review_still_blocks(db, teacher, make_student) -> None:
    sa, sub = await _ai_failed(db, teacher, make_student, ai_optional=False)
    res = await settle.settle(db, sa)
    assert res.new_status == SubmissionStatus.AI_REVIEW_FAILED


async def test_pre_squad_solo_submissions_keep_their_quiz_points(db, teacher, make_student):
    """Squad mates who each handed in alone before the squad existed (solo submissions,
    legacy attempts with no student_id) keep the quiz points those passes earned — the
    squad mean must not drop them for lacking a student_id (prod: squad 8, lab1)."""
    sa_a, sa_b, sub_a = await squad_of_two(
        db, teacher, make_student, config=CFG, scores={"report": 4, "accepted": 1}
    )
    sub_a.squad_id = None
    sub_a.status = SubmissionStatus.COMPLETED
    sub_b = Submission(
        students_assignment_id=sa_b.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=SubmissionStatus.COMPLETED,
        plugin_config_id=sub_a.plugin_config_id,
        test_results={"skipped": True, "reason": "requirements"},
    )
    db.add(sub_b)
    await db.commit()
    await attempt(db, sub_a, None, score=15, max_score=15, passed=True)
    await attempt(db, sub_b, None, score=15, max_score=15, passed=True)

    for sa in (sa_a, sa_b):
        asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
        facts, *_ = await settle.gather_facts(db, sa, asg)
        assert facts.quiz == ("done", 100.0)
