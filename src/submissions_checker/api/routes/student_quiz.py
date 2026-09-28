"""Student quiz-taking routes."""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import (
    AirRaid,
    AppSettings,
    DBSession,
    StudentId,
    StudentUser,
)
from submissions_checker.core import metrics
from submissions_checker.core.i18n import get_vocab
from submissions_checker.core.logging import get_logger
from submissions_checker.core.rate_limit import client_ip
from submissions_checker.core.state_machine import transition
from submissions_checker.core.templates import render
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAnswer,
    QuizAttempt,
    QuizAttemptEvent,
    QuizAttemptPause,
    QuizAttemptSnapshot,
    Student,
    StudentAssignment,
    Submission,
    SubmissionStatus,
    User,
)
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    QuizAttemptStatus,
    QuizDisputeStatus,
    QuizEventOutcome,
)
from submissions_checker.db.models.quiz_dispute import QuizQuestionDispute
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services import squads
from submissions_checker.services.air_raid.base import AirRaidProviderError
from submissions_checker.services.air_raid.geo import resolve_region
from submissions_checker.services.audit import audit
from submissions_checker.services.grading import finalize_grade
from submissions_checker.services.notification_service import push_notification
from submissions_checker.services.quiz_events import (
    INFORMATIONAL_EVENT_TYPES,
    MAX_EVENTS_STORED_PER_ATTEMPT,
    sanitize_ctx,
)
from submissions_checker.services.quiz_grants import effective_max_attempts
from submissions_checker.services.quiz_scoring import (
    apply_question_overrides,
    load_question_overrides,
    score_attempt,
)
from submissions_checker.services.quiz_split import split_draw
from submissions_checker.services.storage import StorageService
from submissions_checker.workers.tasks.notification_tasks import (
    enqueue_teacher_review_notification,
    push_dispute_notifications,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/portal", tags=["student-quiz"])

_TERMINAL_STATUSES = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)

# Proctoring snapshot upload limits
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024  # 2 MB
_ALLOWED_SNAPSHOT_TYPES = {"image/jpeg", "image/png", "image/webp"}
# The declared content-type is client-supplied; the leading bytes decide what is stored.
_SNAPSHOT_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG",),
    "image/webp": (b"RIFF",),  # plus "WEBP" at offset 8, checked separately
}
# One attempt cannot fill object storage on its own.
_MAX_SNAPSHOTS_PER_ATTEMPT = 200

# Anti-cheat event names become JSONB keys and teacher-visible labels: short snake_case
# only, and never the handler's own underscore-prefixed bookkeeping keys.
_EVENT_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_MAX_EVENT_BODY_BYTES = 4096
_MAX_DISTINCT_EVENT_TYPES = 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _needs_consent(db: Any, student_id: int) -> bool:
    """True if the student has not yet acknowledged the proctoring recording notice."""
    consented = await db.scalar(
        select(Student.recording_consent_at).where(Student.id == student_id)
    )
    return consented is None


def _open_pause_seconds(attempt: QuizAttempt) -> float:
    """Seconds elapsed inside the pause that is currently open, or 0 when not paused."""
    paused_at: datetime | None = getattr(attempt, "paused_at", None)
    if paused_at is None:
        return 0.0
    return (_utcnow() - paused_at.replace(tzinfo=UTC)).total_seconds()


def _effective_now(attempt: QuizAttempt) -> datetime:
    """Wall clock, frozen at the moment the attempt was paused for an air raid.

    Every quiz clock in this module is a delta from a stored timestamp, so freezing "now"
    is what stops all of them at once — there is no second place a duration accumulates.
    """
    return _utcnow() - timedelta(seconds=_open_pause_seconds(attempt))


def _close_open_pause(attempt: QuizAttempt) -> int:
    """End an open pause, folding its length into ``paused_seconds``. Returns that length.

    The per-question anchor is walked FORWARD by the pause, the mirror image of the
    ``reduce_time`` violation penalty that walks it backwards. That is what makes a resumed
    question clock continue at exactly the value the student left it at, and it composes
    with a penalty taken before the pause rather than cancelling it.
    """
    if getattr(attempt, "paused_at", None) is None:
        return 0
    elapsed = int(_open_pause_seconds(attempt))
    attempt.paused_seconds = (attempt.paused_seconds or 0) + elapsed
    if attempt.question_started_at is not None:
        attempt.question_started_at = attempt.question_started_at.replace(tzinfo=UTC) + timedelta(
            seconds=elapsed
        )
    attempt.paused_at = None
    return elapsed


def _elapsed_seconds(attempt: QuizAttempt) -> float:
    """Quiz time consumed so far, with every air-raid pause taken out.

    The frozen "now" removes the pause that is currently open; ``paused_seconds`` removes
    all the ones that have already closed.
    """
    wall = (_effective_now(attempt) - attempt.started_at.replace(tzinfo=UTC)).total_seconds()
    return wall - (getattr(attempt, "paused_seconds", 0) or 0)


def _time_penalty(attempt: QuizAttempt) -> int:
    return int((attempt.violations or {}).get("_time_penalty_seconds", 0))


def _is_timed_out(attempt: QuizAttempt) -> bool:
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return False
    effective_seconds = int(limit) * 60 - _time_penalty(attempt)
    return _elapsed_seconds(attempt) > effective_seconds


def _seconds_remaining(attempt: QuizAttempt) -> int | None:
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return None
    effective_limit = limit * 60 - _time_penalty(attempt)
    return max(0, int(effective_limit - _elapsed_seconds(attempt)))


def _seconds_remaining_from_violations(
    attempt: QuizAttempt, violations: dict[str, Any]
) -> int | None:
    """Pure helper used inside report_violation before the DB commit."""
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return None
    penalty = int(violations.get("_time_penalty_seconds", 0))
    effective_limit = limit * 60 - penalty
    elapsed = _elapsed_seconds(attempt)
    return max(0, int(effective_limit - elapsed))


def _is_stepped(attempt: QuizAttempt) -> bool:
    return bool(attempt.config_snapshot.get("per_question_timing"))


def _current_question(attempt: QuizAttempt) -> dict[str, Any] | None:
    questions: list[dict[str, Any]] = attempt.questions_snapshot or []
    if attempt.current_index >= len(questions):
        return None
    return questions[attempt.current_index]


def _question_seconds_remaining(attempt: QuizAttempt) -> int | None:
    """Seconds left on the current question, or None when it carries no limit."""
    question = _current_question(attempt)
    if question is None:
        return None
    limit = question.get("time_limit_seconds")
    if not limit or attempt.question_started_at is None:
        return None
    elapsed = (
        _effective_now(attempt) - attempt.question_started_at.replace(tzinfo=UTC)
    ).total_seconds()
    # Ceil, so a question opens showing its full limit rather than one second short.
    return max(0, ceil(int(limit) - elapsed))


async def _open_pause(db: DBSession, attempt_id: int) -> QuizAttemptPause | None:
    """The pause row that is still open for this attempt, if any."""
    result = await db.execute(
        select(QuizAttemptPause)
        .where(QuizAttemptPause.attempt_id == attempt_id, QuizAttemptPause.ended_at.is_(None))
        .order_by(QuizAttemptPause.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _disputed_question_ids(db: DBSession, attempt_id: int) -> set[int]:
    """Questions of this attempt the student has already reported, for the badge."""
    rows = await db.execute(
        select(QuizQuestionDispute.question_id).where(QuizQuestionDispute.attempt_id == attempt_id)
    )
    return set(rows.scalars().all())


def _assert_attempt_owner(
    attempt: QuizAttempt, submission: Submission | None, student_id: int
) -> None:
    """The attempt belongs to this student — directly, or (legacy NULL rows) via the
    submission's own students_assignment owner."""
    owner = attempt.student_id
    if owner is None and submission is not None:
        owner = submission.students_assignment.student_id
    if submission is None or owner != student_id:
        raise HTTPException(status_code=403)


async def _squad_ctx(
    db: DBSession, attempt: QuizAttempt, submission: Submission, student_id: int
) -> dict[str, Any] | None:
    """Context for the "your part" banner on the squad member's quiz/result pages, or
    None when this attempt isn't part of a squad quiz."""
    info = attempt.config_snapshot.get("squad")
    if not info or submission.squad_id is None:
        return None
    states = await squads.member_quiz_states(db, submission)
    return {
        "my_count": len(attempt.questions_snapshot or []),
        "total": info.get("total_questions"),
        "partners": [m.full_name for m in states if m.student_id != student_id],
        "complete": all(m.passed for m in states),
    }


def _record_timed_out(attempt: QuizAttempt, db: DBSession) -> None:
    """Burn the current question: zero points, flagged as lost to the clock."""
    question = _current_question(attempt)
    if question is None:
        return
    answer = QuizAnswer(
        attempt_id=attempt.id,
        question_id=question["id"],
        answer={},
        is_correct=False,
        points_earned=0,
        timed_out=True,
    )
    db.add(answer)
    attempt.answers.append(answer)


def _advance_expired(attempt: QuizAttempt, db: DBSession) -> int:
    """Burn every question whose window has closed, and return how many were burned.

    Loops rather than handling one question, because the student may have been away for
    longer than a single window — the clock runs whether or not their browser is open.
    Questions with no limit never expire, so the loop stops at the first of those.
    """
    # Belt and braces: the frozen clock already makes a paused window un-expirable, but a
    # pause must never cost the student a question even if that arithmetic is ever wrong.
    if getattr(attempt, "paused_at", None) is not None:
        return 0

    burned = 0
    now = _effective_now(attempt)
    questions: list[dict[str, Any]] = attempt.questions_snapshot or []
    while attempt.current_index < len(questions):
        question = questions[attempt.current_index]
        limit = question.get("time_limit_seconds")
        if not limit:
            break
        if attempt.question_started_at is None:
            attempt.question_started_at = now
            break
        elapsed = (now - attempt.question_started_at.replace(tzinfo=UTC)).total_seconds()
        if elapsed <= limit:
            break
        _record_timed_out(attempt, db)
        attempt.current_index += 1
        # The next window starts when this one ENDED, not now — otherwise a student who
        # closes the laptop for an hour loses exactly one question and gets a fresh clock
        # on the next. Answering normally restarts the clock at "now"; expiring does not.
        attempt.question_started_at = attempt.question_started_at.replace(tzinfo=UTC) + timedelta(
            seconds=limit
        )
        burned += 1
    return burned


_SUPPORTED_QUESTION_TYPES = frozenset(
    {"SINGLE_CHOICE", "MULTIPLE_CHOICE", "ORDERING", "TRUE_FALSE"}
)


def _build_question_config(q_type: str, q: dict[str, Any]) -> dict[str, Any]:
    if q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        # Support both formats:
        #   A) options: ["text1", ...], correct: 0  (or correct: [0,1] for multi)
        #   B) choices: [{"text": "...", "is_correct": true/false}, ...]
        if "choices" in q:
            raw_choices = q["choices"]
            options = [c.get("text", str(c)) for c in raw_choices]
            if q_type == "SINGLE_CHOICE":
                correct_idx = next((i for i, c in enumerate(raw_choices) if c.get("is_correct")), 0)
                return {"options": options, "correct": correct_idx}
            else:
                correct_idxs = [i for i, c in enumerate(raw_choices) if c.get("is_correct")]
                return {"options": options, "correct": correct_idxs}
        if q_type == "SINGLE_CHOICE":
            return {"options": q.get("options", []), "correct": int(q.get("correct", 0))}
        return {"options": q.get("options", []), "correct": [int(x) for x in q.get("correct", [])]}
    if q_type == "ORDERING":
        return {
            "items": q.get("items", []),
            "correct_order": [int(x) for x in q.get("correct_order", [])],
        }
    if q_type == "TRUE_FALSE":
        return {"correct": bool(q.get("correct", False))}
    return {}


def _snapshot_questions(quiz_cfg: dict[str, Any], ids: list[int]) -> list[dict[str, Any]]:
    """Snapshot the config questions with the given 0-based ids, in that order."""
    questions_raw: list[dict[str, Any]] = quiz_cfg.get("questions", [])
    default_seconds = quiz_cfg.get("question_time_default_seconds")
    shuffle_opts = bool(quiz_cfg.get("shuffle_options", True))

    snapshot: list[dict[str, Any]] = []
    for orig_idx in ids:
        q = questions_raw[orig_idx]
        q_type = str(q.get("type", "")).upper()
        q_config = _build_question_config(q_type, q)

        raw_seconds = q.get("time_limit_seconds", default_seconds)
        q_snap: dict[str, Any] = {
            "id": orig_idx,
            "type": q_type,
            "text": str(q.get("text", "")),
            "points": int(q.get("points", 1)),
            "is_required": bool(q.get("required", False)),
            # Question value → quiz-level default → no limit. Resolved once, at draw time, so
            # a later config edit cannot change the clock of an attempt already running.
            "time_limit_seconds": int(raw_seconds) if raw_seconds else None,
            "config": q_config,
        }

        if shuffle_opts and q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            options = list(q_config.get("options", []))
            indices = list(range(len(options)))
            random.shuffle(indices)
            shuffled_options = [options[i] for i in indices]
            if q_type == "SINGLE_CHOICE":
                correct_index = q_config.get("correct", 0)
                q_snap["config"] = {
                    "options": shuffled_options,
                    "correct": indices.index(correct_index),
                }
            else:
                correct_indices = set(q_config.get("correct", []))
                remapped = [i for i, orig in enumerate(indices) if orig in correct_indices]
                q_snap["config"] = {"options": shuffled_options, "correct": sorted(remapped)}

        snapshot.append(q_snap)
    return snapshot


def _select_question_ids(quiz_cfg: dict[str, Any]) -> list[int]:
    """The existing draw: required first, then a shuffled slice of optional ones."""
    questions_raw: list[dict[str, Any]] = quiz_cfg.get("questions", [])
    total = int(quiz_cfg.get("questions_to_send", len(questions_raw)))
    shuffle_q = bool(quiz_cfg.get("shuffle_questions", True))

    # Config apply refuses unknown types now, but a config stored earlier may still
    # carry one (short_answer); drawing it would show a question with no input that
    # still counts toward max_score. Ids stay the config index, so skipping is safe.
    indexed = [
        (i, q)
        for i, q in enumerate(questions_raw)
        if str(q.get("type", "")).upper() in _SUPPORTED_QUESTION_TYPES
    ]
    required = [i for i, q in indexed if q.get("required")]
    optional = [i for i, q in indexed if not q.get("required")]

    if shuffle_q:
        random.shuffle(optional)

    selected = required + optional[: max(0, total - len(required))]

    if shuffle_q:
        random.shuffle(selected)

    return selected


def _build_questions_from_config(quiz_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Select and snapshot questions from config quiz section.

    Each question's ``id`` is its 0-based index in the config ``questions`` list so
    grading can reference it without DB rows.
    """
    return _snapshot_questions(quiz_cfg, _select_question_ids(quiz_cfg))


async def _draw_for_member(
    db: DBSession,
    submission: Submission,
    squad: Any,
    quiz_cfg: dict[str, Any],
    student_id: int,
    *,
    retry: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The member's slice of the squad's shared draw (first attempt) or a fresh draw of
    the same size (retry). The shared draw is written once, under a row lock, by
    whichever member opens the quiz first."""
    # A plain locked select() re-selects a row already in the identity map: SQLAlchemy
    # keeps the pre-lock attribute values instead of reading what the lock just acquired.
    # refresh(..., with_for_update=True) forces the reload.
    await db.refresh(submission, attribute_names=["source_metadata"], with_for_update=True)
    meta = dict(submission.source_metadata or {})
    member_order = sorted(m.student_id for m in squad.members)
    draw = meta.get("squad_quiz_draw")
    if not draw:
        ids = _select_question_ids(quiz_cfg)
        slices = split_draw(_snapshot_questions(quiz_cfg, ids), len(member_order))
        draw = {"question_ids": ids, "slices": slices, "member_order": member_order}
        meta["squad_quiz_draw"] = draw
        submission.source_metadata = meta  # reassign: JSONB change tracking
        await db.flush()
    if student_id not in draw["member_order"]:
        # The squad was edited (teacher re-assign, member removed) after this draw was
        # written — the member_order snapshot no longer includes this student.
        raise HTTPException(
            status_code=409,
            detail="Squad changed after the quiz draw; ask your teacher to reset the draw.",
        )
    idx = draw["member_order"].index(student_id)
    my_ids: list[int] = list(draw["slices"][idx])
    draw_cfg = quiz_cfg
    if retry:
        # Only the required questions that were in THIS member's original slice stay
        # required for the retry draw — otherwise a config with more required questions
        # than the slice size would make `_select_question_ids` return more questions
        # than the member's slice ever held (it always includes every required question).
        mine = set(my_ids)
        draw_cfg = {
            **quiz_cfg,
            "questions_to_send": len(mine),
            "questions": [
                {**q, "required": bool(q.get("required")) and i in mine}
                for i, q in enumerate(quiz_cfg.get("questions", []))
            ],
        }
        my_ids = _select_question_ids(draw_cfg)
    elif bool(quiz_cfg.get("shuffle_questions", True)):
        random.shuffle(my_ids)
    squad_snapshot = {
        "member_index": idx,
        "member_count": len(draw["member_order"]),
        "total_questions": len(draw["question_ids"]),
    }
    # `draw_cfg` (not `quiz_cfg`) so a retry's snapshot reports "required" scoped to this
    # draw — otherwise a filler question pulled in only because a sibling was already
    # required-and-in-slice would still show as required, when this draw treats it as
    # optional filler.
    return _snapshot_questions(draw_cfg, my_ids), squad_snapshot


def _grade_answer(
    q_snap: dict[str, Any],
    raw_answer: Any,
) -> tuple[dict[str, Any], bool | None, int]:
    """Grade a single answer. Returns (answer_jsonb, is_correct, points_earned).

    ``is_correct`` is ``None`` only for a snapshot type the grader does not know;
    config-apply rejects such types, so this is a defensive fallback.
    """
    q_type = q_snap["type"]
    q_config = q_snap["config"]
    q_points = q_snap["points"]

    if q_type == "SINGLE_CHOICE":
        try:
            selected = int(raw_answer)
        except (ValueError, TypeError):
            return {"selected": None}, False, 0
        is_correct = selected == q_config.get("correct", -1)
        return {"selected": selected}, is_correct, q_points if is_correct else 0

    elif q_type == "MULTIPLE_CHOICE":
        if isinstance(raw_answer, list):
            values = raw_answer
        elif raw_answer:
            values = [raw_answer]
        else:
            values = []
        selected_values: list[int]
        try:
            selected_values = sorted(int(v) for v in values)
        except (ValueError, TypeError):
            selected_values = []
        is_correct = selected_values == sorted(q_config.get("correct", []))
        return {"selected": selected_values}, is_correct, q_points if is_correct else 0

    elif q_type == "ORDERING":
        raw_str = raw_answer or ""
        try:
            order = [int(x.strip()) for x in str(raw_str).split(",") if x.strip()]
        except (ValueError, TypeError):
            order = []
        is_correct = order == q_config.get("correct_order", [])
        return {"order": order}, is_correct, q_points if is_correct else 0

    elif q_type == "TRUE_FALSE":
        student_bool = str(raw_answer).lower() == "true"
        is_correct = student_bool == q_config.get("correct", False)
        return {"value": student_bool}, is_correct, q_points if is_correct else 0

    return {"raw": str(raw_answer)}, False, 0


async def _count_used_attempts(
    db: DBSession, submission_id: int, exclude_id: int, student_id: int | None
) -> int:
    """Count this student's finished (non-passing) attempts for a submission, excluding
    the given id. Squad-shared submissions carry every member's attempts, so this is
    always scoped to one student (plus legacy NULL rows, which predate per-student ids
    and belong to a solo submission by this same student)."""
    result = await db.execute(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.submission_id == submission_id,
            QuizAttempt.status.in_(
                [
                    QuizAttemptStatus.COMPLETED,
                    QuizAttemptStatus.TIMED_OUT,
                    QuizAttemptStatus.VIOLATION_FAIL,
                ]
            ),
            QuizAttempt.id != exclude_id,
            or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
        )
    )
    return result.scalar_one() or 0


async def _grade_and_finalize(
    attempt: QuizAttempt,
    db: DBSession,
    status: QuizAttemptStatus = QuizAttemptStatus.COMPLETED,
) -> None:
    # A question a teacher has already ruled broken is credited here, so an attempt that
    # was in progress when the ruling landed needs no separate regrade.
    overrides = await load_question_overrides(
        db, attempt.plugin_config_id, attempt.plugin_config_version
    )
    if apply_question_overrides(attempt, overrides, db):
        await db.flush()

    score, max_score, is_passed, force_failed = score_attempt(attempt)
    if force_failed:
        status = QuizAttemptStatus.VIOLATION_FAIL

    attempt.score = score
    attempt.max_score = max_score
    attempt.is_passed = is_passed
    attempt.submitted_at = _utcnow()
    attempt.status = status
    metrics.quiz_attempts_finished_total.labels(status=status.value).inc()
    if is_passed:
        metrics.quiz_attempts_passed_total.inc()
    # A terminal attempt is never "paused". Fold any open pause into the total and clear the
    # flag, or the frozen clock would outlive the attempt that needed it.
    _close_open_pause(attempt)

    attempts_left: int | None = None
    submission = attempt.submission
    # Only a submission still sitting at QUIZ_SENT can be moved by a quiz outcome. Anything
    # else (already completed, already handed to a teacher) means a stale attempt finishing
    # late; record the attempt but leave the submission where it is rather than 500-ing the
    # student with an InvalidTransitionError.
    if submission and submission.status == SubmissionStatus.QUIZ_SENT:
        if is_passed:
            # The submission may be shared by a squad, so a passing attempt is not enough on
            # its own — `flush` first so `quiz_complete` sees this attempt's `is_passed` write.
            await db.flush()
            # Serialise finalisation on the submission row: two squad-mates passing at the
            # same instant must not both observe QUIZ_SENT and both transition. Whichever
            # gets the lock first finishes; the loser's refreshed status is authoritative,
            # not the (possibly stale) status read at the top of this function.
            await db.refresh(submission, attribute_names=["status"], with_for_update=True)
            if submission.status == SubmissionStatus.QUIZ_SENT and await squads.quiz_complete(
                db, submission
            ):
                # `quiz_then_teacher` hands the attached work to the teacher instead of
                # completing here; the grade is still computed from the quiz, but only once
                # they approve.
                if attempt.config_snapshot.get("review_mode") == "quiz_then_teacher":
                    transition(submission, "quiz_passed_teacher")
                    await enqueue_teacher_review_notification(db, submission.id)
                else:
                    transition(submission, "quiz_passed")
                    await finalize_grade(db, submission)
            elif submission.squad_id is not None:
                await _notify_partners_passed(db, submission, attempt.student_id)
        else:
            max_attempts = effective_max_attempts(
                attempt.config_snapshot.get("max_quiz_attempts"), submission, attempt.student_id
            )
            if max_attempts is not None:
                prior = await _count_used_attempts(
                    db, attempt.submission_id, attempt.id, attempt.student_id
                )
                if prior + 1 >= max_attempts:
                    transition(submission, "quiz_failed")
                    attempts_left = 0
                else:
                    attempts_left = max_attempts - (prior + 1)
            # else: leave at QUIZ_SENT so student can retry

    db.add(
        OutboxMessage(
            event_type=OutboxEventType.QUIZ_RESULT,
            state=OutboxMessageState.PENDING,
            payload={
                "submission_id": attempt.submission_id,
                "attempt_id": attempt.id,
                "score": score,
                "max_score": max_score,
                "is_passed": is_passed,
                "attempts_left": attempts_left,
                "student_id": attempt.student_id,
            },
        )
    )

    await db.commit()


async def _notify_partners_passed(
    db: DBSession, submission: Submission, student_id: int | None
) -> None:
    """Tell the rest of the squad that one member cleared their half — the grade still
    waits on everyone."""
    states = await squads.member_quiz_states(db, submission)
    me = next((m for m in states if m.student_id == student_id), None)
    if me is None:
        return
    vocab = get_vocab(None).get("squad", {})
    for m in states:
        if m.student_id == student_id:
            continue
        user_id = await db.scalar(select(User.id).where(User.student_id == m.student_id))
        if user_id is not None:
            await push_notification(
                db,
                user_id,
                str(vocab.get("notif_partner_passed_title", "")),
                str(vocab.get("notif_partner_passed_body", "")).format(name=me.full_name),
                "/portal",
            )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/subjects/{subject_id}/assignments/{sa_id}/quiz")
async def start_or_resume_quiz(
    subject_id: int,
    sa_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    """Create or resume a QuizAttempt, drawing questions from the pinned plugin config."""
    if await _needs_consent(db, student_id):
        return RedirectResponse(url="/portal/consent", status_code=303)

    sa_result = await db.execute(
        select(StudentAssignment)
        .where(StudentAssignment.id == sa_id, StudentAssignment.student_id == student_id)
        .options(
            selectinload(StudentAssignment.submissions),
            selectinload(StudentAssignment.subjects_assignment),
        )
    )
    sa = sa_result.scalar_one_or_none()
    if sa is None:
        raise HTTPException(status_code=404)

    latest_sub = await squads.latest_submission(db, sa)
    if latest_sub is None or latest_sub.status != SubmissionStatus.QUIZ_SENT:
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")

    attempts_result = await db.execute(
        select(QuizAttempt)
        .where(
            QuizAttempt.submission_id == latest_sub.id,
            or_(QuizAttempt.student_id == student_id, QuizAttempt.student_id.is_(None)),
        )
        .order_by(QuizAttempt.started_at)
    )
    existing = list(attempts_result.scalars().all())

    in_progress = next((a for a in existing if a.status == QuizAttemptStatus.IN_PROGRESS), None)
    if in_progress:
        return RedirectResponse(url=f"/portal/quiz/{in_progress.id}", status_code=303)

    passed = next((a for a in existing if a.is_passed), None)
    if passed:
        return RedirectResponse(url=f"/portal/quiz/{passed.id}/result", status_code=303)

    # Load plugin config pinned for this submission
    if not latest_sub.plugin_config_id:
        raise HTTPException(status_code=503, detail="No plugin config pinned to this submission")
    config_record = await db.get(SubjectPluginConfig, latest_sub.plugin_config_id)
    if config_record is None:
        raise HTTPException(status_code=503, detail="Plugin config not found")

    assignment_code = sa.subjects_assignment.code
    if not assignment_code:
        raise HTTPException(status_code=404, detail="Assignment has no config code")

    plugin_assignment: dict[str, Any] = config_record.config.get("assignments", {}).get(
        assignment_code, {}
    )
    quiz_cfg: dict[str, Any] = plugin_assignment.get("quiz", {})
    if not quiz_cfg or not quiz_cfg.get("questions"):
        raise HTTPException(status_code=404, detail="No quiz configured for this assignment")

    # Check max attempts — the config's cap plus anything a teacher granted this student.
    max_attempts = quiz_cfg.get("max_quiz_attempts")
    allowed = effective_max_attempts(max_attempts, latest_sub, student_id)
    used_count = sum(1 for a in existing if a.status in _TERMINAL_STATUSES)
    if allowed is not None and used_count >= allowed:
        latest_finished = existing[-1] if existing else None
        if latest_finished:
            return RedirectResponse(
                url=f"/portal/quiz/{latest_finished.id}/result", status_code=303
            )
        raise HTTPException(status_code=403, detail="No quiz attempts remaining")

    squad = await squads.squad_for_submission(db, latest_sub)
    squad_snapshot: dict[str, Any] | None = None
    if squad is None:
        questions_snapshot = _build_questions_from_config(quiz_cfg)
    else:
        questions_snapshot, squad_snapshot = await _draw_for_member(
            db, latest_sub, squad, quiz_cfg, student_id, retry=used_count > 0
        )
    config_snapshot: dict[str, Any] = {
        "pass_threshold_pct": float(quiz_cfg.get("pass_threshold_pct", 0.6)),
        "show_correct_answers_after": bool(quiz_cfg.get("show_correct_answers_after", False)),
        "anti_cheat": quiz_cfg.get("anti_cheat", {}),
        # Snapshotted so a config re-upload mid-attempt cannot change where a pass lands.
        "review_mode": plugin_assignment.get("review_mode", "tests_only"),
    }
    if squad_snapshot is not None:
        config_snapshot["squad"] = squad_snapshot
    if max_attempts is not None:
        config_snapshot["max_quiz_attempts"] = int(max_attempts)
    if quiz_cfg.get("time_limit_minutes") is not None:
        config_snapshot["time_limit_minutes"] = int(quiz_cfg["time_limit_minutes"])
    # Stepper mode is derived, never declared: if anything actually drawn carries a clock, this
    # attempt is answered one question at a time. Recorded per attempt so the delivery style
    # cannot change under a student mid-quiz.
    if any(q.get("time_limit_seconds") for q in questions_snapshot):
        config_snapshot["per_question_timing"] = True

    now = _utcnow()
    attempt = QuizAttempt(
        submission_id=latest_sub.id,
        student_id=student_id,
        plugin_config_id=config_record.id,
        plugin_config_version=config_record.version,
        questions_snapshot=questions_snapshot,
        config_snapshot=config_snapshot,
        started_at=now,
        status=QuizAttemptStatus.IN_PROGRESS,
        current_index=0,
        question_started_at=now if config_snapshot.get("per_question_timing") else None,
    )
    db.add(attempt)
    await db.commit()
    metrics.quiz_attempts_started_total.inc()
    await db.refresh(attempt)

    return RedirectResponse(url=f"/portal/quiz/{attempt.id}", status_code=303)


@router.get("/quiz/{attempt_id}", response_class=HTMLResponse, response_model=None)
async def show_quiz(
    request: Request,
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse | RedirectResponse:
    if await _needs_consent(db, student_id):
        return RedirectResponse(url="/portal/consent", status_code=303)

    attempt = await db.get(
        QuizAttempt,
        attempt_id,
        options=[selectinload(QuizAttempt.answers)],
    )
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if (attempt.violations or {}).get("_force_fail"):
        await _grade_and_finalize(attempt, db, status=QuizAttemptStatus.VIOLATION_FAIL)
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if attempt.paused_at is not None:
        # Paused for an air raid. This page carries no question text, no options and no
        # answer form: pausing must not become a way to read a question at leisure. It also
        # does NOT include the anti-cheat partial, which is what suspends the camera gate
        # and every violation reporter while the student is in a shelter.
        pause = await _open_pause(db, attempt.id)
        return render(
            request,
            "student_quiz_paused.html",
            {
                "current_user": current_user,
                "attempt": attempt,
                "seconds_remaining": (
                    _question_seconds_remaining(attempt)
                    if _is_stepped(attempt)
                    else _seconds_remaining(attempt)
                ),
                "question_number": attempt.current_index + 1,
                "total_questions": len(attempt.questions_snapshot or []),
                "is_stepped": _is_stepped(attempt),
                "paused_since": attempt.paused_at,
                "region_title": pause.region_title if pause else None,
            },
        )

    if _is_timed_out(attempt):
        await _grade_and_finalize(attempt, db, status=QuizAttemptStatus.TIMED_OUT)
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    anti_cheat_config = attempt.config_snapshot.get("anti_cheat", {})
    proctoring_config = anti_cheat_config.get("camera", {})

    if _is_stepped(attempt):
        # Burn anything whose window closed while they were away, then either finish the
        # attempt or serve the question they are actually on.
        if _advance_expired(attempt, db):
            await db.commit()
        question = _current_question(attempt)
        if question is None:
            await _grade_and_finalize(attempt, db)
            return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)
        if attempt.question_started_at is None:
            attempt.question_started_at = _utcnow()
            await db.commit()
        return render(
            request,
            "student_quiz_step.html",
            {
                "current_user": current_user,
                "attempt": attempt,
                "question": question,
                "question_number": attempt.current_index + 1,
                "total_questions": len(attempt.questions_snapshot or []),
                "seconds_remaining": _question_seconds_remaining(attempt),
                "anti_cheat_config": anti_cheat_config,
                "proctoring_config": proctoring_config,
                "disputed_ids": await _disputed_question_ids(db, attempt.id),
                "squad_ctx": await _squad_ctx(db, attempt, submission, student_id),
            },
        )

    existing_answers = {a.question_id: a.answer for a in attempt.answers}
    seconds_remaining = _seconds_remaining(attempt)

    return render(
        request,
        "student_quiz.html",
        {
            "current_user": current_user,
            "attempt": attempt,
            "questions": attempt.questions_snapshot,
            "existing_answers": existing_answers,
            "seconds_remaining": seconds_remaining,
            "anti_cheat_config": anti_cheat_config,
            "proctoring_config": proctoring_config,
            "disputed_ids": await _disputed_question_ids(db, attempt.id),
            "squad_ctx": await _squad_ctx(db, attempt, submission, student_id),
        },
    )


async def _read_event(request: Request) -> tuple[str, dict[str, Any]]:
    """Parse the anti-cheat event body: a well-formed ``type`` plus an optional ``ctx``.

    A bad ``type`` is refused (400) as before; a bad ``ctx`` is dropped to ``{}`` so the event
    itself is always recorded.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_EVENT_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Event body too large")
    raw = await request.body()
    if len(raw) > _MAX_EVENT_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Event body too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    event_type = data.get("type")
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
        raise HTTPException(status_code=400, detail="type must be a short snake_case name")
    return event_type, sanitize_ctx(data.get("ctx"))


def _looks_like(content_type: str, data: bytes) -> bool:
    """Whether the leading bytes match the declared image type."""
    if not any(data.startswith(magic) for magic in _SNAPSHOT_MAGIC.get(content_type, ())):
        return False
    if content_type == "image/webp":
        return data[8:12] == b"WEBP"
    return True


async def _record_event(
    db: DBSession,
    request: Request,
    attempt: QuizAttempt,
    student_id: int,
    event_type: str,
    ctx: dict[str, Any],
    outcome: QuizEventOutcome,
    *,
    action: str = "none",
    count_after: int | None = None,
    rule_threshold: int | None = None,
) -> None:
    """Log the event and, below the per-attempt cap, add its timeline row (caller commits)."""
    log = logger.warning if action != "none" else logger.info
    log(
        "quiz_anticheat_event",
        attempt_id=attempt.id,
        student_id=student_id,
        event_type=event_type,
        outcome=outcome.value,
        action=action,
        count_after=count_after,
        rule_threshold=rule_threshold,
        ctx=ctx,
    )
    stored = await db.scalar(
        select(func.count())
        .select_from(QuizAttemptEvent)
        .where(QuizAttemptEvent.attempt_id == attempt.id)
    )
    if (stored or 0) >= MAX_EVENTS_STORED_PER_ATTEMPT:
        return
    db.add(
        QuizAttemptEvent(
            attempt_id=attempt.id,
            event_type=event_type,
            count_after=count_after,
            rule_threshold=rule_threshold,
            action=action,
            outcome=outcome,
            client_ctx=ctx,
            ip=client_ip(request)[:64],
            user_agent=(request.headers.get("user-agent") or "")[:256] or None,
        )
    )


@router.post("/quiz/{attempt_id}/event")
async def report_violation(
    attempt_id: int,
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> JSONResponse:
    """Receive a client-side anti-cheat event, evaluate configured rules, and record it on the
    attempt's timeline."""
    event_type, ctx = await _read_event(request)

    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status != QuizAttemptStatus.IN_PROGRESS:
        await _record_event(
            db,
            request,
            attempt,
            student_id,
            event_type,
            ctx,
            QuizEventOutcome.IGNORED_NOT_IN_PROGRESS,
        )
        await db.commit()
        return JSONResponse({"action": "none", "violation_count": 0})

    if event_type in INFORMATIONAL_EVENT_TYPES:
        # "How long were they away" — evidence for the teacher, never a violation.
        await _record_event(
            db, request, attempt, student_id, event_type, ctx, QuizEventOutcome.INFORMATIONAL
        )
        await db.commit()
        return JSONResponse(
            {"action": "none", "seconds_remaining": None, "message": "", "violation_count": 0}
        )

    if attempt.paused_at is not None:
        # Anti-cheat is suspended during an air-raid pause. Returning before `violations` is
        # touched is the point: a student running for a shelter must not accumulate
        # tab-switch counts, let alone cross a _force_fail threshold.
        await _record_event(
            db, request, attempt, student_id, event_type, ctx, QuizEventOutcome.IGNORED_PAUSED
        )
        await db.commit()
        return JSONResponse({"action": "none", "violation_count": 0, "paused": True})

    violations = dict(attempt.violations or {})
    if event_type not in violations and len(violations) >= _MAX_DISTINCT_EVENT_TYPES:
        # A client can invent names; the blob must not grow without bound. Nothing is
        # recorded and no rule can match a name the config never mentions anyway.
        await _record_event(
            db, request, attempt, student_id, event_type, ctx, QuizEventOutcome.IGNORED_TYPE_CAP
        )
        await db.commit()
        return JSONResponse(
            {"action": "none", "seconds_remaining": None, "message": "", "violation_count": 0}
        )
    violations[event_type] = violations.get(event_type, 0) + 1
    count = int(violations[event_type])

    anti_cheat = attempt.config_snapshot.get("anti_cheat", {})

    fail_threshold = next(
        (
            r["threshold"]
            for r in anti_cheat.get("rules", [])
            if r["event"] == event_type and r["action"]["type"] == "fail"
        ),
        None,
    )

    response_action = "none"
    seconds_remaining = None
    message = ""
    matched_threshold: int | None = None

    for rule in anti_cheat.get("rules", []):
        if rule["event"] == event_type and count >= rule["threshold"]:
            matched_threshold = int(rule["threshold"])
            action = rule["action"]
            action_type = action["type"]
            penalty = int(action.get("penalty_seconds", 60))
            raw_msg = action.get("message", "")

            message = raw_msg.format(
                count=count,
                threshold=rule["threshold"],
                penalty_seconds=penalty,
                fail_threshold=fail_threshold if fail_threshold is not None else "?",
            )

            if action_type == "fail":
                violations["_force_fail"] = True
                response_action = "fail"

            elif action_type == "reduce_time":
                response_action = "reduce_time"
                if _is_stepped(attempt):
                    # There is no attempt-wide budget to deduct from — shorten the question
                    # they are on by walking its start time backwards. If that exhausts it,
                    # the next page load burns the question like any other expiry.
                    if attempt.question_started_at is not None:
                        attempt.question_started_at = attempt.question_started_at - timedelta(
                            seconds=penalty
                        )
                    seconds_remaining = _question_seconds_remaining(attempt)
                else:
                    violations["_time_penalty_seconds"] = (
                        int(violations.get("_time_penalty_seconds", 0)) + penalty
                    )
                    seconds_remaining = _seconds_remaining_from_violations(attempt, violations)

            elif action_type == "warn":
                response_action = "warn"

            elif action_type == "flag":
                flagged = violations.setdefault("_flagged_events", [])
                if isinstance(flagged, list):
                    flagged.append(event_type)
                response_action = "flag"
                message = ""

            break

    attempt.violations = violations
    await _record_event(
        db,
        request,
        attempt,
        student_id,
        event_type,
        ctx,
        QuizEventOutcome.APPLIED,
        action=response_action,
        count_after=count,
        rule_threshold=matched_threshold,
    )
    if response_action == "fail":
        logger.warning(
            "quiz_attempt_force_failed",
            attempt_id=attempt.id,
            student_id=student_id,
            event_type=event_type,
            count_after=count,
            rule_threshold=matched_threshold,
            violations={k: v for k, v in violations.items() if not k.startswith("_")},
        )
    await db.commit()

    return JSONResponse(
        {
            "action": response_action,
            "seconds_remaining": seconds_remaining,
            "message": message,
            "violation_count": count,
        }
    )


@router.post("/quiz/{attempt_id}/dispute")
async def report_question(
    attempt_id: int,
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> JSONResponse:
    """Report a question as incorrect or invalid, for a teacher to rule on later.

    Deliberately non-blocking, and deliberately free of every side effect the other quiz
    endpoints carry: it does not advance the stepper, does not call ``_advance_expired``,
    does not touch ``violations``, and does not restart any clock. Flagging a question must
    cost the student nothing — otherwise nobody would risk pressing the button mid-exam.

    Accepted in any attempt state: the same endpoint serves the live quiz and the result
    page, because an appeal is most often filed after seeing the score.
    """
    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    raw_question_id = body.get("question_id")
    try:
        question_id = int(raw_question_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="question_id is required") from None

    # The snapshot is a random draw, so a question the student never saw is not theirs to
    # flag — and crediting it later would reach attempts this one has no standing over.
    q_snap = next(
        (q for q in (attempt.questions_snapshot or []) if q.get("id") == question_id), None
    )
    if q_snap is None:
        raise HTTPException(status_code=400, detail="Question is not part of this attempt")

    note = str(body.get("note") or "").strip() or None

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
    await db.flush()

    student = await db.get(Student, student_id)
    await push_dispute_notifications(
        db,
        submission.id,
        dispute_id=dispute.id,
        question_text=str(q_snap.get("text", "")),
        student_name=(student.full_name if student else f"Student {student_id}"),
    )
    await audit(
        db,
        action="student_reported_question",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="quiz_attempt",
        target_id=attempt.id,
        dispute_id=dispute.id,
        question_id=question_id,
        note=note,
    )
    await db.commit()
    metrics.disputes_opened_total.inc()

    return JSONResponse({"ok": True, "dispute_id": dispute.id})


@router.post("/quiz/{attempt_id}/airraid/pause")
async def request_air_raid_pause(
    attempt_id: int,
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
    air_raid: AirRaid,
) -> JSONResponse:
    """Pause the attempt if an air-raid alert is really active over the student's location.

    Refusals come back as HTTP 200 with a ``reason``, because every one of them is a normal
    outcome the page has to explain rather than an error. Nothing is paused on an unverified
    claim: no coordinates, no coverage, no alert and no reachable provider all mean no pause.

    Geolocation denial never reaches here — the browser reports that to the student directly
    and posts nothing, which also means a request with no coordinates is simply a 400.
    """
    attempt = await db.get(QuizAttempt, attempt_id, options=[selectinload(QuizAttempt.answers)])
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status != QuizAttemptStatus.IN_PROGRESS:
        return JSONResponse({"paused": False, "reason": "attempt_closed"})

    if attempt.paused_at is not None:
        # Double click, or a second tab. Idempotent.
        return JSONResponse({"paused": True, "already": True})

    # Settle the clock BEFORE stopping it. Otherwise a student could pause at 0:00 and
    # freeze a window that has in fact already closed.
    if _is_timed_out(attempt):
        await _grade_and_finalize(attempt, db, status=QuizAttemptStatus.TIMED_OUT)
        return JSONResponse({"paused": False, "reason": "attempt_closed"})
    if _is_stepped(attempt):
        if _advance_expired(attempt, db):
            await db.commit()
        if _current_question(attempt) is None:
            await _grade_and_finalize(attempt, db)
            return JSONResponse({"paused": False, "reason": "attempt_closed"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="lat and lng are required") from None

    if air_raid is None:
        return JSONResponse({"paused": False, "reason": "unavailable"})

    region = resolve_region(lat, lng)
    if region is None:
        return JSONResponse({"paused": False, "reason": "outside_coverage"})

    try:
        alert = await air_raid.active_alert(region.uid)
    except AirRaidProviderError:
        # An outage must neither pause nor brick the quiz; the student keeps answering.
        logger.warning("air_raid_check_unavailable", attempt_id=attempt_id, region=region.uid)
        return JSONResponse({"paused": False, "reason": "unavailable"})

    if alert is None:
        return JSONResponse({"paused": False, "reason": "no_alert", "region": region.title})

    now = _utcnow()
    attempt.paused_at = now
    db.add(
        QuizAttemptPause(
            attempt_id=attempt.id,
            started_at=now,
            latitude=Decimal(str(round(lat, 6))),
            longitude=Decimal(str(round(lng, 6))),
            region_uid=region.uid,
            region_title=region.title,
            alert_started_at=alert.started_at,
        )
    )
    await audit(
        db,
        action="student_air_raid_pause",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="quiz_attempt",
        target_id=attempt.id,
        region_uid=region.uid,
        region_title=region.title,
        latitude=round(lat, 6),
        longitude=round(lng, 6),
    )
    await db.commit()
    metrics.air_raid_pauses_total.inc()

    logger.info("air_raid_pause_started", attempt_id=attempt.id, region=region.uid)
    return JSONResponse(
        {"paused": True, "region": region.title, "redirect": f"/portal/quiz/{attempt.id}"}
    )


@router.post("/quiz/{attempt_id}/airraid/resume")
async def resume_after_air_raid(
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    """Resume a paused attempt. The student decides when they are safe; nothing re-checks.

    A plain form POST, so it works with JavaScript disabled, and the redirect re-renders the
    question page — which re-includes the anti-cheat partial. That re-inclusion IS the
    re-arming of proctoring.
    """
    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.paused_at is None:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)

    pause = await _open_pause(db, attempt.id)
    paused_at = attempt.paused_at
    elapsed = _close_open_pause(attempt)
    if pause is not None:
        pause.ended_at = paused_at.replace(tzinfo=UTC) + timedelta(seconds=elapsed)

    await audit(
        db,
        action="student_air_raid_resume",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="quiz_attempt",
        target_id=attempt.id,
        paused_seconds=elapsed,
    )
    await db.commit()

    logger.info("air_raid_pause_ended", attempt_id=attempt.id, paused_seconds=elapsed)
    return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)


@router.post("/quiz/{attempt_id}/snapshot")
async def upload_snapshot(
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
    settings: AppSettings,
    event_type: str,
    frame: UploadFile = File(...),
) -> JSONResponse:
    """Store a webcam evidence frame captured client-side on a flagged proctoring event."""
    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status != QuizAttemptStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Attempt is not in progress")

    if attempt.paused_at is not None:
        # Proctoring is suspended during a pause: nothing is stored, and 200 keeps the
        # best-effort client uploader quiet rather than making it retry.
        return JSONResponse({"stored": False, "paused": True})

    camera = attempt.config_snapshot.get("anti_cheat", {}).get("camera", {})
    if not camera.get("capture_snapshots"):
        raise HTTPException(status_code=403, detail="Snapshot capture is not enabled")

    if frame.content_type not in _ALLOWED_SNAPSHOT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported image type")

    data = await frame.read()
    if not data or len(data) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Frame missing or too large")
    if not _looks_like(frame.content_type, data):
        raise HTTPException(status_code=415, detail="Frame bytes do not match the image type")

    storage = StorageService(settings) if settings.s3_endpoint_url else None
    if storage is None:
        # Storage not configured — accept silently so proctoring never blocks the quiz.
        return JSONResponse({"stored": False})

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[frame.content_type]
    seq = await db.scalar(
        select(func.count())
        .select_from(QuizAttemptSnapshot)
        .where(QuizAttemptSnapshot.attempt_id == attempt_id)
    )
    if (seq or 0) >= _MAX_SNAPSHOTS_PER_ATTEMPT:
        # Dropped, not refused: 200 keeps the best-effort uploader from retrying.
        return JSONResponse({"stored": False, "reason": "limit"})
    safe_event = "".join(c for c in event_type if c.isalnum() or c in "_-")[:48] or "event"
    key = f"proctoring/attempt-{attempt_id}/{(seq or 0) + 1}-{safe_event}.{ext}"
    url = await storage.upload_bytes(data, key, frame.content_type)

    snapshot = QuizAttemptSnapshot(
        attempt_id=attempt_id,
        event_type=safe_event,
        s3_key=key,
        s3_url=url,
        captured_at=_utcnow(),
    )
    db.add(snapshot)
    await db.commit()

    # No URL in the response: evidence is private and is read back only through the
    # authenticated teacher endpoint. Handing the client an object-storage URL would
    # reintroduce exactly the public exposure this avoids.
    return JSONResponse({"stored": True})


@router.post("/quiz/{attempt_id}/answer")
async def answer_question(
    request: Request,
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    """Record the answer to the current question of a stepped attempt and advance.

    Always redirects (PRG), so a refresh cannot replay an answer, and a stale tab posting an
    old question index changes nothing.
    """
    attempt = await db.get(
        QuizAttempt,
        attempt_id,
        options=[
            selectinload(QuizAttempt.answers),
            selectinload(QuizAttempt.submission).selectinload(Submission.students_assignment),
        ],
    )
    if attempt is None:
        raise HTTPException(status_code=404)

    submission = attempt.submission
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if not _is_stepped(attempt):
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)

    if attempt.paused_at is not None:
        # A stale tab posting mid-pause. Record nothing and burn nothing — answering with
        # the clock stopped would be exactly the abuse the pause must not enable.
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)

    if (attempt.violations or {}).get("_force_fail"):
        await _grade_and_finalize(attempt, db, status=QuizAttemptStatus.VIOLATION_FAIL)
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    form = await request.form()

    # Burn anything that expired before this POST landed. If that consumed the question this
    # form was for, the answer is already recorded as timed out and must not be graded again.
    expired = _advance_expired(attempt, db)
    try:
        posted_index = int(str(form.get("index", "-1")))
    except ValueError:
        posted_index = -1

    if expired == 0 and posted_index == attempt.current_index:
        q_snap = _current_question(attempt)
        if q_snap is not None:
            q_id = q_snap["id"]
            q_type = q_snap["type"]
            if q_type == "MULTIPLE_CHOICE":
                raw: Any = list(form.getlist(f"answer_{q_id}"))
            elif q_type == "ORDERING":
                raw = form.get(f"answer_ordering_{q_id}", "")
            else:
                raw = form.get(f"answer_{q_id}", "")

            answer_json, is_correct, points_earned = _grade_answer(q_snap, raw)
            answer = QuizAnswer(
                attempt_id=attempt_id,
                question_id=q_id,
                answer=answer_json,
                is_correct=is_correct,
                points_earned=points_earned,
                timed_out=False,
            )
            db.add(answer)
            metrics.quiz_answers_total.inc()
            attempt.answers.append(answer)
            attempt.current_index += 1
            attempt.question_started_at = _utcnow()

    if _current_question(attempt) is None:
        await db.flush()
        await _grade_and_finalize(attempt, db)
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    await db.commit()
    return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)


@router.post("/quiz/{attempt_id}/submit")
async def submit_quiz(
    request: Request,
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    attempt = await db.get(
        QuizAttempt,
        attempt_id,
        options=[
            selectinload(QuizAttempt.answers),
            selectinload(QuizAttempt.submission).selectinload(Submission.students_assignment),
        ],
    )
    if attempt is None:
        raise HTTPException(status_code=404)

    submission = attempt.submission
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if _is_stepped(attempt):
        # A stepped attempt is finalized by its last question, not by a bulk submit. Anything
        # posting here is a stale form, so send them back to the question they are actually on.
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)

    if attempt.paused_at is not None:
        # Before the answer-wiping regrade below: a stale submit arriving during a pause
        # would otherwise delete everything the student had already answered.
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}", status_code=303)

    if (attempt.violations or {}).get("_force_fail"):
        final_status = QuizAttemptStatus.VIOLATION_FAIL
    elif _is_timed_out(attempt):
        final_status = QuizAttemptStatus.TIMED_OUT
    else:
        final_status = QuizAttemptStatus.COMPLETED

    form = await request.form()

    for existing in list(attempt.answers):
        await db.delete(existing)
    await db.flush()

    new_answers: list[QuizAnswer] = []
    for q_snap in attempt.questions_snapshot:
        q_id = q_snap["id"]
        q_type = q_snap["type"]

        # A multi-choice field arrives as a list, the rest as a single value;
        # `_grade_answer` accepts either and normalises per question type.
        raw: Any
        if q_type == "MULTIPLE_CHOICE":
            raw = list(form.getlist(f"answer_{q_id}"))
        elif q_type == "ORDERING":
            raw = form.get(f"answer_ordering_{q_id}", "")
        else:
            raw = form.get(f"answer_{q_id}", "")

        answer_json, is_correct, points_earned = _grade_answer(q_snap, raw)
        new_answers.append(
            QuizAnswer(
                attempt_id=attempt_id,
                question_id=q_id,
                answer=answer_json,
                is_correct=is_correct,
                points_earned=points_earned,
            )
        )

    for ans in new_answers:
        db.add(ans)
    await db.flush()

    await db.refresh(attempt)
    attempt_answers_result = await db.execute(
        select(QuizAnswer).where(QuizAnswer.attempt_id == attempt_id)
    )
    attempt.answers = list(attempt_answers_result.scalars().all())

    await _grade_and_finalize(attempt, db, status=final_status)
    return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)


@router.get("/quiz/{attempt_id}/result", response_class=HTMLResponse)
async def quiz_result(
    request: Request,
    attempt_id: int,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> HTMLResponse:
    attempt = await db.get(
        QuizAttempt,
        attempt_id,
        options=[
            selectinload(QuizAttempt.answers),
            selectinload(QuizAttempt.submission).selectinload(Submission.students_assignment),
        ],
    )
    if attempt is None:
        raise HTTPException(status_code=404)

    submission = attempt.submission
    _assert_attempt_owner(attempt, submission, student_id)
    assert (
        submission is not None
    )  # narrows for mypy; _assert_attempt_owner already raised otherwise

    if attempt.status == QuizAttemptStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Quiz not yet submitted")

    answers_by_qid = {a.question_id: a for a in attempt.answers}
    show_correct = attempt.config_snapshot.get("show_correct_answers_after", False)

    question_results = []
    for q_snap in attempt.questions_snapshot:
        ans = answers_by_qid.get(q_snap["id"])
        question_results.append(
            {
                "question": q_snap,
                "answer": ans.answer if ans else None,
                "is_correct": ans.is_correct if ans else None,
                "points_earned": ans.points_earned if ans else 0,
                "timed_out": bool(ans.timed_out) if ans else False,
            }
        )

    sa = submission.students_assignment
    sa_full_result = await db.execute(
        select(StudentAssignment)
        .where(StudentAssignment.id == sa.id)
        .options(selectinload(StudentAssignment.subjects_assignment))
    )
    sa_full = sa_full_result.scalar_one()
    subject_id = sa_full.subjects_assignment.subject_id

    # For a squad member who didn't upload, ``sa`` belongs to whoever did — the "back to
    # assignment" link must point at this student's own StudentAssignment instead.
    my_sa_id = await db.scalar(
        select(StudentAssignment.id).where(
            StudentAssignment.student_id == student_id,
            StudentAssignment.subjects_assignment_id == sa_full.subjects_assignment_id,
        )
    )

    return render(
        request,
        "student_quiz_result.html",
        {
            "current_user": current_user,
            "attempt": attempt,
            "question_results": question_results,
            "timed_out_count": sum(1 for r in question_results if r["timed_out"]),
            "show_correct": show_correct,
            "subject_id": subject_id,
            "student_assignment_id": my_sa_id or sa.id,
            "disputed_ids": await _disputed_question_ids(db, attempt.id),
            "squad_ctx": await _squad_ctx(db, attempt, submission, student_id),
        },
    )
