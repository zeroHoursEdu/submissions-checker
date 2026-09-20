"""Teacher-granted extra quiz attempts.

A student who uses up ``max_quiz_attempts`` without passing sends the submission to
``FAILED``. A teacher may hand that one student one more attempt. The grant is recorded per
student in ``submissions.source_metadata["quiz_extra_attempts"]`` (so a fresh upload starts
clean and squad members are counted apart), raises the effective attempt cap everywhere the
cap is read, and moves the submission back to ``QUIZ_SENT`` through an explicit event.
Nothing here touches ``quiz_attempts`` rows — history stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.enums import QuizAttemptStatus, SubmissionStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads

EXTRA_KEY = "quiz_extra_attempts"

_TERMINAL = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)
_GRANTABLE_STATUSES = (SubmissionStatus.FAILED, SubmissionStatus.QUIZ_SENT)


class GrantError(Exception):
    """The (submission, student) pair is not stuck on an exhausted quiz."""


def extra_attempts(submission: Any, student_id: int | None) -> int:
    """Extra attempts granted to this student on this submission (0 when none)."""
    meta = submission.source_metadata or {}
    extras = meta.get(EXTRA_KEY) or {}
    return int(extras.get(str(student_id), 0))


def effective_max_attempts(base: int | None, submission: Any, student_id: int | None) -> int | None:
    """The config's cap plus every grant; ``None`` stays ``None`` (no cap at all)."""
    if base is None:
        return None
    return int(base) + extra_attempts(submission, student_id)


def is_exhausted(attempts: Sequence[Any], effective_max: int | None) -> bool:
    """True when the student has used every attempt they are allowed and none passed.

    Needs at least one attempt, every attempt terminal (nothing still running), no pass,
    and a cap to exhaust — an uncapped quiz can never be "used up".
    """
    if effective_max is None or not attempts:
        return False
    if any(a.status not in _TERMINAL for a in attempts):
        return False
    if any(a.is_passed for a in attempts):
        return False
    return len(attempts) >= effective_max


async def base_max_attempts(db: AsyncSession, submission: Submission) -> int | None:
    """``max_quiz_attempts`` from the config pinned to this submission, or None."""
    if submission.plugin_config_id is None:
        return None
    code = await db.scalar(
        select(SubjectsAssignment.code)
        .join(StudentAssignment, StudentAssignment.subjects_assignment_id == SubjectsAssignment.id)
        .where(StudentAssignment.id == submission.students_assignment_id)
    )
    cfg = await db.get(SubjectPluginConfig, submission.plugin_config_id)
    if cfg is None or not code:
        return None
    quiz = cfg.config.get("assignments", {}).get(code, {}).get("quiz") or {}
    value = quiz.get("max_quiz_attempts")
    return int(value) if value is not None else None


async def _students_on(db: AsyncSession, submission: Submission) -> list[int]:
    """Solo: the owner. Squad: every currently enrolled member (unenrolled ones no longer
    block completion, so they cannot be granted anything either)."""
    squad = await squads.squad_for_submission(db, submission)
    if squad is None:
        owner = await db.scalar(
            select(StudentAssignment.student_id).where(
                StudentAssignment.id == submission.students_assignment_id
            )
        )
        return [owner] if owner is not None else []
    member_ids = {m.student_id for m in squad.members}
    rows = await db.execute(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == squad.subject_id,
            SubjectsStudents.student_id.in_(member_ids),
        )
    )
    return sorted(sid for (sid,) in rows)


async def _attempts_of(
    db: AsyncSession, submission: Submission, student_id: int
) -> list[QuizAttempt]:
    # Same filter as the student quiz gate: this student's rows plus legacy NULL rows,
    # which predate per-student ids and belong to a solo submission by this student.
    rows = await db.execute(
        select(QuizAttempt).where(
            QuizAttempt.submission_id == submission.id,
            or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
        )
    )
    return list(rows.scalars().all())


async def _is_grantable(
    db: AsyncSession, submission: Submission, student_id: int, base: int | None
) -> bool:
    if submission.status not in _GRANTABLE_STATUSES or base is None:
        return False
    attempts = await _attempts_of(db, submission, student_id)
    return is_exhausted(attempts, effective_max_attempts(base, submission, student_id))


async def grantable_students(db: AsyncSession, submission: Submission) -> list[int]:
    """Students on this submission who are stuck on an exhausted quiz."""
    base = await base_max_attempts(db, submission)
    if base is None:
        return []
    out: list[int] = []
    for sid in await _students_on(db, submission):
        if await _is_grantable(db, submission, sid, base):
            out.append(sid)
    return out


async def grant_extra_attempt(db: AsyncSession, submission: Submission, student_id: int) -> int:
    """Give ``student_id`` one more attempt on ``submission``; returns their new extra total.

    Raises GrantError unless the pair is grantable. Does not commit — caller owns the
    transaction, as with the other teacher unstick controls.
    """
    base = await base_max_attempts(db, submission)
    if student_id not in await _students_on(db, submission) or not await _is_grantable(
        db, submission, student_id, base
    ):
        raise GrantError("student is not stuck on an exhausted quiz for this submission")
    meta = dict(submission.source_metadata or {})
    extras = dict(meta.get(EXTRA_KEY) or {})
    total = int(extras.get(str(student_id), 0)) + 1
    extras[str(student_id)] = total
    meta[EXTRA_KEY] = extras
    submission.source_metadata = meta  # reassign: JSONB change tracking
    transition(submission, "quiz_attempt_granted")
    return total
