"""Open a quiz with nothing uploaded (``review_mode: quiz_and_teacher_scores``).

The quiz machinery hangs off a Submission; in this mode the student never uploads, so the
first time they open the quiz an upload-less QUIZ_ONLY submission is created and moved
straight to QUIZ_SENT. An existing submission (an old ZIP on prod, a squad-mate's) is
reused. Also the two questions the quiz-outcome paths ask about a scored submission.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    Submission,
)
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads, teacher_scores


class QuizOpenError(Exception):
    """The quiz cannot be opened from here (the message is the HTTP detail)."""


async def open_quiz_submission(
    db: AsyncSession, sa: StudentAssignment, student_id: int
) -> Submission:
    """The submission the student's quiz hangs off, creating a QUIZ_ONLY one if none exists.

    Locks the student's assignment row first so a double click cannot create two. Does not
    commit — the caller's attempt creation commits both together.
    """
    await db.execute(
        select(StudentAssignment.id).where(StudentAssignment.id == sa.id).with_for_update()
    )
    existing = await squads.latest_submission(db, sa)
    if existing is not None:
        return existing
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None or not teacher_scores.is_scored_mode(asg.config):
        raise QuizOpenError("Quiz not available for this submission")
    subject = await db.get(Subject, asg.subject_id)
    squad = None
    if subject is not None and subject.squad_max_size is not None:
        if await squads.has_pending_invites(
            db, asg.subject_id, student_id
        ) or await squads.squad_has_pending_invites(db, asg.subject_id, student_id):
            raise QuizOpenError("Squad invitation pending; answer or cancel it first.")
        squad = await squads.squad_of(db, asg.subject_id, student_id)
    config_id = await db.scalar(
        select(SubjectPluginConfig.id)
        .where(SubjectPluginConfig.subject_id == asg.subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    if config_id is None:
        raise QuizOpenError("No plugin config for this subject")
    sub = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.QUIZ_ONLY,
        source_metadata={},
        status=SubmissionStatus.PENDING,
        plugin_config_id=config_id,
        squad_id=squad.id if squad else None,
        test_results={"skipped": True, "reason": teacher_scores.MODE},
    )
    db.add(sub)
    await db.flush()
    if squad:
        squads.lock_on_submit(squad)
    transition(sub, "quiz_opened")
    return sub


async def _assignment_of(
    db: AsyncSession, submission: Submission
) -> tuple[StudentAssignment, SubjectsAssignment] | None:
    sa = await db.get(StudentAssignment, submission.students_assignment_id)
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id) if sa else None
    if sa is None or asg is None:
        return None
    return sa, asg


async def is_scored_submission(db: AsyncSession, submission: Submission) -> bool:
    """True when the submission's assignment is *currently* quiz_and_teacher_scores."""
    pair = await _assignment_of(db, submission)
    return pair is not None and teacher_scores.is_scored_mode(pair[1].config)


async def scores_complete_for(db: AsyncSession, submission: Submission) -> bool:
    """True when the teacher has entered every required criterion for this submission."""
    pair = await _assignment_of(db, submission)
    if pair is None:
        return False
    sa, asg = pair
    crits = teacher_scores.criteria((asg.config or {}).get("grading"))
    return teacher_scores.is_complete(crits, sa.teacher_scores)
