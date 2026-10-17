"""Arrange helpers for requirements-model tests (settle, the board, the quiz routes).

One subject, one assignment ``l1`` whose config is a new-style ``requirements:`` dict, the
grade range ``0..sum(points)``, and an upload-less QUIZ_ONLY submission when asked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import (
    ClassroomLinkMethod,
    LLMGradingStatus,
    QuizAttemptStatus,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.squad import Squad, SquadMember
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig

CFG: dict[str, Any] = {
    "source": "classroom",
    "requirements": [
        {"type": "quiz", "key": "quiz", "points": 8},
        {"type": "score", "key": "report", "title": "Звіт", "max": 5},
        {"type": "check", "key": "accepted", "title": "Прийнято", "points": 0},
    ],
}


def quiz_with(max_attempts: int | None) -> dict[str, Any]:
    return {
        "questions": [
            {"type": "single_choice", "text": "q", "points": 1, "options": ["w", "r"], "correct": 1}
        ],
        "shuffle_questions": False,
        "shuffle_options": False,
        "pass_threshold_pct": 0.6,
        "max_quiz_attempts": max_attempts,
    }


def span_of(config: dict[str, Any]) -> int:
    """Sum of the requirement points (a score item's ``max`` is its points)."""
    return sum(int(r.get("max", r.get("points", 0))) for r in config["requirements"])


async def _subject_and_assignment(
    db, teacher, *, config: dict[str, Any], max_attempts: int | None, squad_max_size=None
) -> tuple[Subject, SubjectsAssignment, int]:
    subject = Subject(name="Reqland", owner_id=teacher.id, squad_max_size=squad_max_size)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    a_cfg = dict(config)
    if any(r.get("type") == "quiz" for r in config["requirements"]):
        a_cfg["quiz"] = quiz_with(max_attempts)
    plugin_cfg = SubjectPluginConfig(
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
        max_grade=span_of(config),
        config=config,
    )
    db.add_all([plugin_cfg, asg])
    await db.commit()
    await db.refresh(asg)
    await db.refresh(plugin_cfg)
    return subject, asg, plugin_cfg.id


async def _enrolled(db, make_student, subject: Subject, asg: SubjectsAssignment, *, name, scores):
    student = await make_student(full_name=name)
    student.recording_consent_at = datetime.now(UTC)
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    sa = StudentAssignment(
        student_id=student.id, subjects_assignment_id=asg.id, teacher_scores=scores
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return student, sa


def _upload_less(sa_id: int, config_id: int, status: SubmissionStatus, squad_id=None) -> Submission:
    return Submission(
        students_assignment_id=sa_id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=status,
        plugin_config_id=config_id,
        squad_id=squad_id,
        test_results={"skipped": True, "reason": "requirements"},
    )


async def arrange(
    db,
    teacher,
    make_student,
    *,
    config: dict[str, Any],
    scores: dict[str, int] | None = None,
    status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
    max_attempts: int | None = 2,
    with_submission: bool = True,
):
    """Owned subject, assignment ``l1`` with ``config``, one enrolled consented student whose
    SA row carries ``scores``. Returns ``(subject, asg, student, sa, sub)``, or
    ``(subject, asg, student, sa)`` when ``with_submission`` is False."""
    subject, asg, config_id = await _subject_and_assignment(
        db, teacher, config=config, max_attempts=max_attempts
    )
    student, sa = await _enrolled(db, make_student, subject, asg, name="Valentyn D", scores=scores)
    if not with_submission:
        return subject, asg, student, sa
    sub = _upload_less(sa.id, config_id, status)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return subject, asg, student, sa, sub


async def arrange_second_student(
    db, subject: Subject, asg: SubjectsAssignment, make_student, *, scores=None
) -> tuple[Any, StudentAssignment]:
    """Another enrolled consented student on ``arrange``'s assignment, with an SA row
    carrying ``scores`` and no submission. Returns ``(student, sa)``."""
    return await _enrolled(db, make_student, subject, asg, name="Bohdana S", scores=scores)


async def attempt(
    db,
    sub: Submission,
    student_id: int | None,
    *,
    score: int,
    max_score: int,
    passed: bool,
    status: QuizAttemptStatus = QuizAttemptStatus.COMPLETED,
    commit: bool = True,
) -> QuizAttempt:
    """A finished quiz attempt on ``sub`` by ``student_id`` (committed, or only flushed
    with ``commit=False``)."""
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
        submitted_at=datetime.now(UTC),
        status=status,
        is_passed=passed,
        score=score,
        max_score=max_score,
    )
    db.add(a)
    if not commit:
        await db.flush()
        return a
    await db.commit()
    await db.refresh(a)
    return a


async def squad_of_two(
    db,
    teacher,
    make_student,
    *,
    config: dict[str, Any],
    scores: dict[str, int] | None = None,
    both_passed: bool = False,
    status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
):
    """A locked squad of two on a ``squad_max_size=2`` subject, one shared upload-less
    submission (``squad_id`` set) on the first member's SA, ``scores`` on both SA rows and,
    with ``both_passed``, a full-marks passed attempt per member.
    Returns ``(sa_a, sa_b, sub)``."""
    subject, asg, config_id = await _subject_and_assignment(
        db, teacher, config=config, max_attempts=2, squad_max_size=2
    )
    (st_a, sa_a) = await _enrolled(db, make_student, subject, asg, name="Anna A", scores=scores)
    (st_b, sa_b) = await _enrolled(db, make_student, subject, asg, name="Bohdan B", scores=scores)
    squad = Squad(subject_id=subject.id, created_by_user_id=teacher.id, locked_at=datetime.now(UTC))
    db.add(squad)
    await db.flush()
    db.add_all(
        [
            SquadMember(squad_id=squad.id, student_id=st_a.id, subject_id=subject.id),
            SquadMember(squad_id=squad.id, student_id=st_b.id, subject_id=subject.id),
        ]
    )
    sub = _upload_less(sa_a.id, config_id, status, squad_id=squad.id)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    if both_passed:
        for sid in (st_a.id, st_b.id):
            await attempt(db, sub, sid, score=1, max_score=1, passed=True)
    return sa_a, sa_b, sub


async def submissions_of(db, sa: StudentAssignment) -> list[Submission]:
    rows = await db.execute(
        select(Submission).where(Submission.students_assignment_id == sa.id).order_by(Submission.id)
    )
    return list(rows.scalars().all())


async def start_and_answer_correctly(client, subject_id: int, sa_id: int) -> int:
    """Open the quiz over HTTP as the authenticated student and submit every question with
    the right option (``quiz_with``'s single question: option 1). Returns the attempt id.

    The bulk ``/submit`` re-reads every answer from its form, so answering and submitting
    are one post."""
    r = await client.get(
        f"/portal/subjects/{subject_id}/assignments/{sa_id}/quiz", follow_redirects=False
    )
    assert r.status_code == 303 and "/portal/quiz/" in r.headers["location"], r.text
    attempt_id = int(r.headers["location"].rsplit("/", 1)[-1])
    r = await client.post(
        f"/portal/quiz/{attempt_id}/submit", data={"answer_0": "1"}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return attempt_id


async def add_llm_draft(db, asg: SubjectsAssignment, student, *, points: dict[str, int]) -> None:
    """A confirmed Classroom link and one work for ``student`` whose LLM grading carries an
    unsaved draft with ``points`` per criterion (what a student must never see)."""
    link = ClassroomStudentLink(
        subject_id=asg.subject_id,
        classroom_user_id=f"u{student.id}",
        classroom_name="Draft Student",
        student_id=student.id,
        method=ClassroomLinkMethod.EMAIL.value,
        confirmed=True,
    )
    db.add(link)
    await db.commit()
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=link.id,
        classroom_submission_id=f"w{student.id}",
        state="TURNED_IN",
        late=False,
        content_hash="d".ljust(64, "0"),
        manifest=[],
        seen_at=datetime.now(UTC),
    )
    db.add(work)
    await db.commit()
    draft = {
        "criteria": {
            k: {"points": v, "justification": "j", "evidence": "e"} for k, v in points.items()
        },
        "comment": "c",
        "usage": {},
    }
    db.add(LLMGrading(classroom_work_id=work.id, status=LLMGradingStatus.DONE.value, draft=draft))
    await db.commit()
