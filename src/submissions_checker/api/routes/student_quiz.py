"""Student quiz-taking routes."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import AppSettings, DBSession, StudentId, StudentUser
from submissions_checker.core.state_machine import transition
from submissions_checker.core.templates import render
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAnswer,
    QuizAttempt,
    QuizAttemptSnapshot,
    Student,
    StudentAssignment,
    Submission,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    QuizAttemptStatus,
)
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.services.grading import finalize_grade
from submissions_checker.services.storage import StorageService
from submissions_checker.workers.tasks.notification_tasks import (
    enqueue_teacher_review_notification,
)

router = APIRouter(prefix="/portal", tags=["student-quiz"])

_TERMINAL_STATUSES = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)

# Proctoring snapshot upload limits
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024  # 2 MB
_ALLOWED_SNAPSHOT_TYPES = {"image/jpeg", "image/png", "image/webp"}


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


def _elapsed_seconds(attempt: QuizAttempt) -> float:
    return (_utcnow() - attempt.started_at.replace(tzinfo=UTC)).total_seconds()


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
    elapsed = (_utcnow() - attempt.question_started_at.replace(tzinfo=UTC)).total_seconds()
    # Ceil, so a question opens showing its full limit rather than one second short.
    return max(0, ceil(int(limit) - elapsed))


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
    burned = 0
    questions: list[dict[str, Any]] = attempt.questions_snapshot or []
    while attempt.current_index < len(questions):
        question = questions[attempt.current_index]
        limit = question.get("time_limit_seconds")
        if not limit:
            break
        if attempt.question_started_at is None:
            attempt.question_started_at = _utcnow()
            break
        elapsed = (_utcnow() - attempt.question_started_at.replace(tzinfo=UTC)).total_seconds()
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


def _build_questions_from_config(quiz_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Select and snapshot questions from config quiz section.

    Each question's ``id`` is its 0-based index in the config ``questions`` list so
    grading can reference it without DB rows.
    """
    questions_raw: list[dict[str, Any]] = quiz_cfg.get("questions", [])
    total = int(quiz_cfg.get("questions_to_send", len(questions_raw)))
    default_seconds = quiz_cfg.get("question_time_default_seconds")
    shuffle_q = bool(quiz_cfg.get("shuffle_questions", True))
    shuffle_opts = bool(quiz_cfg.get("shuffle_options", True))

    indexed = list(enumerate(questions_raw))
    required = [(i, q) for i, q in indexed if q.get("required")]
    optional = [(i, q) for i, q in indexed if not q.get("required")]

    if shuffle_q:
        random.shuffle(optional)

    remaining = max(0, total - len(required))
    selected = required + optional[:remaining]

    if shuffle_q:
        random.shuffle(selected)

    snapshot: list[dict[str, Any]] = []
    for orig_idx, q in selected:
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


def _grade_answer(
    q_snap: dict[str, Any],
    raw_answer: Any,
) -> tuple[dict[str, Any], bool | None, int]:
    """Grade a single answer. Returns (answer_jsonb, is_correct, points_earned).

    ``is_correct`` is ``None`` for SHORT_ANSWER, which no rule can grade and a
    teacher marks by hand; the column is nullable for exactly that case.
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

    elif q_type == "SHORT_ANSWER":
        text_answer = str(raw_answer).strip() if raw_answer else ""
        return {"text": text_answer}, None, 0

    return {"raw": str(raw_answer)}, False, 0


async def _count_used_attempts(db: DBSession, submission_id: int, exclude_id: int) -> int:
    """Count finished (non-passing) attempts for a submission, excluding the given id."""
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
        )
    )
    return result.scalar_one() or 0


async def _grade_and_finalize(
    attempt: QuizAttempt,
    db: DBSession,
    status: QuizAttemptStatus = QuizAttemptStatus.COMPLETED,
) -> None:
    force_fail = (attempt.violations or {}).get("_force_fail", False)

    if force_fail:
        is_passed = False
        status = QuizAttemptStatus.VIOLATION_FAIL
        score = sum(a.points_earned or 0 for a in attempt.answers)
        max_score = sum(q["points"] for q in attempt.questions_snapshot)
    else:
        score = sum(a.points_earned or 0 for a in attempt.answers)
        max_score = sum(q["points"] for q in attempt.questions_snapshot)
        threshold = attempt.config_snapshot.get("pass_threshold_pct", 0.6)
        is_passed = (score / max_score) >= threshold if max_score > 0 else False

    attempt.score = score
    attempt.max_score = max_score
    attempt.is_passed = is_passed
    attempt.submitted_at = _utcnow()
    attempt.status = status

    attempts_left: int | None = None
    submission = attempt.submission
    # Only a submission still sitting at QUIZ_SENT can be moved by a quiz outcome. Anything
    # else (already completed, already handed to a teacher) means a stale attempt finishing
    # late; record the attempt but leave the submission where it is rather than 500-ing the
    # student with an InvalidTransitionError.
    if submission and submission.status == SubmissionStatus.QUIZ_SENT:
        if is_passed:
            # `quiz_then_teacher` hands the attached work to the teacher instead of completing
            # here; the grade is still computed from the quiz, but only once they approve.
            if attempt.config_snapshot.get("review_mode") == "quiz_then_teacher":
                transition(submission, "quiz_passed_teacher")
                await enqueue_teacher_review_notification(db, submission.id)
            else:
                transition(submission, "quiz_passed")
                await finalize_grade(db, submission)
        else:
            max_attempts = attempt.config_snapshot.get("max_quiz_attempts")
            if max_attempts is not None:
                prior = await _count_used_attempts(db, attempt.submission_id, attempt.id)
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
            },
        )
    )

    await db.commit()


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

    latest_sub = max(sa.submissions, key=lambda s: s.created_at) if sa.submissions else None
    if latest_sub is None or latest_sub.status != SubmissionStatus.QUIZ_SENT:
        raise HTTPException(status_code=403, detail="Quiz not available for this submission")

    attempts_result = await db.execute(
        select(QuizAttempt)
        .where(QuizAttempt.submission_id == latest_sub.id)
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

    # Check max attempts
    max_attempts = quiz_cfg.get("max_quiz_attempts")
    used_count = sum(1 for a in existing if a.status in _TERMINAL_STATUSES)
    if max_attempts is not None and used_count >= max_attempts:
        latest_finished = existing[-1] if existing else None
        if latest_finished:
            return RedirectResponse(
                url=f"/portal/quiz/{latest_finished.id}/result", status_code=303
            )
        raise HTTPException(status_code=403, detail="No quiz attempts remaining")

    questions_snapshot = _build_questions_from_config(quiz_cfg)
    config_snapshot: dict[str, Any] = {
        "pass_threshold_pct": float(quiz_cfg.get("pass_threshold_pct", 0.6)),
        "show_correct_answers_after": bool(quiz_cfg.get("show_correct_answers_after", False)),
        "anti_cheat": quiz_cfg.get("anti_cheat", {}),
        # Snapshotted so a config re-upload mid-attempt cannot change where a pass lands.
        "review_mode": plugin_assignment.get("review_mode", "tests_only"),
    }
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
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if (attempt.violations or {}).get("_force_fail"):
        await _grade_and_finalize(attempt, db, status=QuizAttemptStatus.VIOLATION_FAIL)
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

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
        },
    )


@router.post("/quiz/{attempt_id}/event")
async def report_violation(
    attempt_id: int,
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> JSONResponse:
    """Receive a client-side anti-cheat event and evaluate configured rules."""
    data = await request.json()
    event_type = str(data.get("type", ""))

    attempt = await db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404)

    sub_result = await db.execute(
        select(Submission)
        .where(Submission.id == attempt.submission_id)
        .options(selectinload(Submission.students_assignment))
    )
    submission = sub_result.scalar_one_or_none()
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

    if attempt.status != QuizAttemptStatus.IN_PROGRESS:
        return JSONResponse({"action": "none", "violation_count": 0})

    violations = dict(attempt.violations or {})
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

    for rule in anti_cheat.get("rules", []):
        if rule["event"] == event_type and count >= rule["threshold"]:
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
    await db.commit()

    return JSONResponse(
        {
            "action": response_action,
            "seconds_remaining": seconds_remaining,
            "message": message,
            "violation_count": count,
        }
    )


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
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

    if attempt.status != QuizAttemptStatus.IN_PROGRESS:
        raise HTTPException(status_code=409, detail="Attempt is not in progress")

    camera = attempt.config_snapshot.get("anti_cheat", {}).get("camera", {})
    if not camera.get("capture_snapshots"):
        raise HTTPException(status_code=403, detail="Snapshot capture is not enabled")

    if frame.content_type not in _ALLOWED_SNAPSHOT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported image type")

    data = await frame.read()
    if not data or len(data) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Frame missing or too large")

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
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if not _is_stepped(attempt):
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
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

    if attempt.status in _TERMINAL_STATUSES:
        return RedirectResponse(url=f"/portal/quiz/{attempt_id}/result", status_code=303)

    if _is_stepped(attempt):
        # A stepped attempt is finalized by its last question, not by a bulk submit. Anything
        # posting here is a stale form, so send them back to the question they are actually on.
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
    if submission is None or submission.students_assignment.student_id != student_id:
        raise HTTPException(status_code=403)

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
            "student_assignment_id": sa.id,
        },
    )
