"""Resolving a broken-question dispute, and re-scoring everyone it affected.

The hard part is not the arithmetic — it is reach. A question is a config index, not a row,
and every attempt froze its own shuffled copy of it. "Credit this question for everyone
trapped in it" therefore means: every attempt drawn from the same plugin config version
whose snapshot contains that index.

Nothing here commits. The route owns the single transaction, so a failure part-way through
cannot leave a half-credited cohort.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.core.logging import get_logger
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    QuizAttemptStatus,
    QuizDisputeStatus,
    SubmissionStatus,
)
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.models.quiz_dispute import QuizQuestionDispute, QuizQuestionOverride
from submissions_checker.db.models.quiz_template import QuizAttempt
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.user import User
from submissions_checker.services.grading import finalize_grade
from submissions_checker.services.notification_service import push_notification
from submissions_checker.services.quiz_scoring import apply_question_overrides, score_attempt
from submissions_checker.workers.tasks.notification_tasks import (
    enqueue_teacher_review_notification,
)

logger = get_logger(__name__)

# Attempts that a credit can still change. VIOLATION_FAIL is excluded on purpose: those
# failed for cheating, which is not what the dispute was about.
_REGRADABLE_STATUSES = (QuizAttemptStatus.COMPLETED, QuizAttemptStatus.TIMED_OUT)


@dataclass(frozen=True)
class AttemptRescore:
    """What changed for one attempt."""

    attempt_id: int
    submission_id: int
    student_id: int
    score: int
    max_score: int
    was_passed: bool
    is_passed: bool

    @property
    def flipped(self) -> bool:
        return self.is_passed and not self.was_passed


@dataclass(frozen=True)
class DisputeOutcome:
    accepted: bool
    rescored: list[AttemptRescore]
    resolved_dispute_ids: list[int]


async def regrade_question(
    db: AsyncSession,
    *,
    plugin_config_id: int,
    plugin_config_version: int,
    question_id: int,
) -> list[AttemptRescore]:
    """Re-score every finished attempt that drew this question from this config version.

    Attempts already passing are skipped — there is nothing to win, and moving their
    submission again would be wrong. Attempts still IN_PROGRESS are skipped too: they read
    the override when they finalize, and pre-writing an answer row for them would be
    double-counted the moment the student answered that question for real.

    Does not commit.
    """
    result = await db.execute(
        select(QuizAttempt)
        .where(
            QuizAttempt.plugin_config_id == plugin_config_id,
            QuizAttempt.plugin_config_version == plugin_config_version,
            QuizAttempt.status.in_(_REGRADABLE_STATUSES),
            QuizAttempt.is_passed.is_not(True),
        )
        .options(
            selectinload(QuizAttempt.answers),
            selectinload(QuizAttempt.submission).selectinload(Submission.students_assignment),
        )
    )
    candidates = result.scalars().all()

    rescored: list[AttemptRescore] = []
    for attempt in candidates:
        # The draw is random, so not every attempt of this version saw the question.
        if not any(q.get("id") == question_id for q in attempt.questions_snapshot or []):
            continue

        was_passed = bool(attempt.is_passed)
        apply_question_overrides(attempt, {question_id}, db)
        score, max_score, is_passed, _force_failed = score_attempt(attempt)
        attempt.score = score
        attempt.max_score = max_score
        attempt.is_passed = is_passed
        # `status` and `submitted_at` are history and stay put — an attempt that ran out of
        # time still ran out of time, and rewriting that would corrupt the attempt counter.

        sa: StudentAssignment | None = (
            attempt.submission.students_assignment if attempt.submission else None
        )
        rescored.append(
            AttemptRescore(
                attempt_id=attempt.id,
                submission_id=attempt.submission_id,
                student_id=sa.student_id if sa else 0,
                score=score,
                max_score=max_score,
                was_passed=was_passed,
                is_passed=is_passed,
            )
        )

    await db.flush()
    return rescored


async def _advance_submission(db: AsyncSession, attempt: QuizAttempt) -> None:
    """Move a submission whose quiz outcome just flipped to passing, then re-grade it."""
    submission = attempt.submission
    if submission is None:
        return

    review_mode = attempt.config_snapshot.get("review_mode")
    to_teacher = review_mode == "quiz_then_teacher"
    status = submission.status

    if status == SubmissionStatus.QUIZ_SENT:
        # Never finished. The ordinary quiz edges apply.
        event = "quiz_passed_teacher" if to_teacher else "quiz_passed"
    elif status == SubmissionStatus.FAILED:
        # The terminal-state gap: only an accepted dispute can move a failed submission.
        event = "dispute_regrade_passed_teacher" if to_teacher else "dispute_regrade_passed"
    elif status == SubmissionStatus.COMPLETED:
        # Already passed on some attempt; the quiz component may still have risen.
        await finalize_grade(db, submission)
        return
    elif status == SubmissionStatus.AWAITING_TEACHER_REVIEW:
        # The teacher's approval will call finalize_grade; writing a grade now pre-empts it.
        return
    else:
        logger.warning(
            "dispute_regrade_unexpected_status",
            submission_id=submission.id,
            status=str(status),
        )
        return

    transition(submission, event)
    if to_teacher:
        await enqueue_teacher_review_notification(db, submission.id)
    else:
        await finalize_grade(db, submission)


async def resolve_dispute(
    db: AsyncSession,
    dispute: QuizQuestionDispute,
    *,
    accept: bool,
    note: str,
    resolved_by_user_id: int,
) -> DisputeOutcome:
    """Record a teacher's ruling and, when accepted, credit and re-score the cohort.

    Accepting also closes every other open report on the same question of the same config
    version: disputes are unlimited by design, so one ruling has to answer all of them
    rather than leaving the teacher fifty identical panels.

    Does not commit.
    """
    now = datetime.now(UTC)
    status = QuizDisputeStatus.ACCEPTED if accept else QuizDisputeStatus.REJECTED

    dispute.status = status
    dispute.teacher_note = note
    dispute.resolved_by_user_id = resolved_by_user_id
    dispute.resolved_at = now
    resolved_ids = [dispute.id]

    rescored: list[AttemptRescore] = []

    if (
        accept
        and dispute.plugin_config_id is not None
        and dispute.plugin_config_version is not None
    ):
        await db.execute(
            pg_insert(QuizQuestionOverride)
            .values(
                plugin_config_id=dispute.plugin_config_id,
                plugin_config_version=dispute.plugin_config_version,
                question_id=dispute.question_id,
                credit_all=True,
                dispute_id=dispute.id,
                created_by_user_id=resolved_by_user_id,
                note=note,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_quiz_question_override")
        )

        siblings = await db.execute(
            select(QuizQuestionDispute).where(
                QuizQuestionDispute.plugin_config_id == dispute.plugin_config_id,
                QuizQuestionDispute.plugin_config_version == dispute.plugin_config_version,
                QuizQuestionDispute.question_id == dispute.question_id,
                QuizQuestionDispute.status == QuizDisputeStatus.OPEN,
                QuizQuestionDispute.id != dispute.id,
            )
        )
        for sibling in siblings.scalars().all():
            sibling.status = QuizDisputeStatus.ACCEPTED
            sibling.teacher_note = note
            sibling.resolved_by_user_id = resolved_by_user_id
            sibling.resolved_at = now
            resolved_ids.append(sibling.id)

        rescored = await regrade_question(
            db,
            plugin_config_id=dispute.plugin_config_id,
            plugin_config_version=dispute.plugin_config_version,
            question_id=dispute.question_id,
        )

        for entry in rescored:
            if not entry.flipped:
                continue
            attempt = await db.get(
                QuizAttempt,
                entry.attempt_id,
                options=[selectinload(QuizAttempt.submission)],
            )
            if attempt is not None:
                await _advance_submission(db, attempt)

    await _notify_students(db, dispute, accept=accept, note=note, rescored=rescored)

    logger.info(
        "quiz_dispute_resolved",
        dispute_id=dispute.id,
        accepted=accept,
        rescored=len(rescored),
        also_resolved=len(resolved_ids) - 1,
    )
    return DisputeOutcome(accepted=accept, rescored=rescored, resolved_dispute_ids=resolved_ids)


async def _push_in_app(
    db: AsyncSession,
    student_id: int,
    *,
    attempt_id: int,
    title: str,
    body: str,
) -> None:
    """Notification bell row for the student behind a Student id, if they have a login."""
    user_id = await db.scalar(select(User.id).where(User.student_id == student_id))
    if user_id is None:
        return
    await push_notification(db, user_id, title, body, link=f"/portal/quiz/{attempt_id}/result")


async def _notify_students(
    db: AsyncSession,
    dispute: QuizQuestionDispute,
    *,
    accept: bool,
    note: str,
    rescored: list[AttemptRescore],
) -> None:
    """Tell the student who reported, and every classmate the ruling moved, what happened.

    In-app immediately (so the bell is right the moment the teacher clicks) plus an outbox
    event for the email, which is the path every other student-facing message already takes.
    """
    by_attempt = {entry.attempt_id: entry for entry in rescored}
    reporter = by_attempt.get(dispute.attempt_id)

    if accept:
        title = "Your reported question was accepted"
        body = f"The question was credited to you. Teacher's note: {note}"
    else:
        title = "Your reported question was reviewed"
        body = f"The question stands as written. Teacher's note: {note}"
    await _push_in_app(
        db, dispute.student_id, attempt_id=dispute.attempt_id, title=title, body=body
    )

    db.add(
        OutboxMessage(
            event_type=OutboxEventType.QUIZ_DISPUTE_RESOLVED,
            state=OutboxMessageState.PENDING,
            payload={
                "dispute_id": dispute.id,
                "attempt_id": dispute.attempt_id,
                "question_id": dispute.question_id,
                "decision": "accept" if accept else "reject",
                "note": note,
                "score": reporter.score if reporter else None,
                "max_score": reporter.max_score if reporter else None,
                "is_passed": reporter.is_passed if reporter else None,
            },
        )
    )

    # A classmate who never reported anything but whose result changed is told why.
    for entry in rescored:
        if entry.attempt_id == dispute.attempt_id or not entry.flipped:
            continue
        await _push_in_app(
            db,
            entry.student_id,
            attempt_id=entry.attempt_id,
            title="Your quiz result was recalculated",
            body=(
                "A question on your quiz was found to be incorrect and has been credited "
                f"to everyone who received it. Teacher's note: {note}"
            ),
        )
        db.add(
            OutboxMessage(
                event_type=OutboxEventType.QUIZ_DISPUTE_RESOLVED,
                state=OutboxMessageState.PENDING,
                payload={
                    "dispute_id": None,
                    "attempt_id": entry.attempt_id,
                    "question_id": dispute.question_id,
                    "decision": "regraded",
                    "note": note,
                    "score": entry.score,
                    "max_score": entry.max_score,
                    "is_passed": entry.is_passed,
                },
            )
        )
