"""Open a quiz for an assignment whose plan has a ``quiz`` requirement.

The quiz machinery hangs off a Submission. Requirements are independent, so the quiz opens
whenever its item is pending — before any upload, or with nothing ever uploaded. The
submission the student's attempts already hang off (an upload, a squad-mate's) is reused;
with none, an upload-less QUIZ_ONLY submission is created and moved to QUIZ_SENT. settle
decides the status from there.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    Submission,
)
from submissions_checker.db.models.enums import SubmissionSourceType, SubmissionStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import settle, squads

# An upload that never got through its checks (failed archive or tests) must not trap the
# student: the quiz needs no file, so such a dead end opens a fresh submission — unless the
# student already has attempts on it, which a fresh submission would silently reset.
# An exhausted quiz (FAILED) is NOT reopened — that is the gate; the teacher grants attempts.
REOPENABLE = frozenset({SubmissionStatus.VALIDATION_FAILED, SubmissionStatus.TEST_FAILED})


class QuizOpenError(Exception):
    """The quiz cannot be opened from here (the message is the HTTP detail)."""


async def open_quiz_submission(
    db: AsyncSession, sa: StudentAssignment, student_id: int
) -> Submission:
    """The submission the student's quiz hangs off, creating a QUIZ_ONLY one if none fits.

    Locks the student's assignment row first so a double click cannot create two (NO KEY
    UPDATE, the same lock settle takes: inserting the submission or attempt takes KEY SHARE
    on this row). Does not commit — the caller's attempt creation commits both together.
    """
    await db.execute(
        select(StudentAssignment.id)
        .where(StudentAssignment.id == sa.id)
        .with_for_update(key_share=True)
    )
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None or not settle.plan_for(asg).has("quiz"):
        raise QuizOpenError("Quiz not available for this submission")
    subs, _scope = await settle.scope_submissions(db, sa, asg)
    existing = await settle.quiz_host(db, subs)
    if existing is not None and (
        existing.status not in REOPENABLE or await _has_attempts(db, existing)
    ):
        return existing
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
        test_results={"skipped": True, "reason": "requirements"},
    )
    db.add(sub)
    await db.flush()
    if squad:
        squads.lock_on_submit(squad)
    transition(sub, "awaiting_quiz")  # PENDING -> QUIZ_SENT; settle refines it later
    return sub


async def _has_attempts(db: AsyncSession, submission: Submission) -> bool:
    count = await db.scalar(
        select(func.count(QuizAttempt.id)).where(QuizAttempt.submission_id == submission.id)
    )
    return bool(count)
