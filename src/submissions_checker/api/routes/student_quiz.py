"""Student quiz-taking routes."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import DBSession, StudentId, StudentUser
from submissions_checker.db.models import (
    OutboxMessage,
    QuizAnswer,
    QuizAttempt,
    StudentAssignment,
    Submission,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState, QuizAttemptStatus
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment

router = APIRouter(prefix="/portal", tags=["student-quiz"])
templates = Jinja2Templates(directory="templates")

_TERMINAL_STATUSES = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_seconds(attempt: QuizAttempt) -> float:
    return (_utcnow() - attempt.started_at.replace(tzinfo=timezone.utc)).total_seconds()


def _time_penalty(attempt: QuizAttempt) -> int:
    return int((attempt.violations or {}).get("_time_penalty_seconds", 0))


def _is_timed_out(attempt: QuizAttempt) -> bool:
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return False
    effective_seconds = limit * 60 - _time_penalty(attempt)
    return _elapsed_seconds(attempt) > effective_seconds


def _seconds_remaining(attempt: QuizAttempt) -> int | None:
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return None
    effective_limit = limit * 60 - _time_penalty(attempt)
    return max(0, int(effective_limit - _elapsed_seconds(attempt)))


def _seconds_remaining_from_violations(attempt: QuizAttempt, violations: dict[str, Any]) -> int | None:
    """Pure helper used inside report_violation before the DB commit."""
    limit = attempt.config_snapshot.get("time_limit_minutes")
    if not limit:
        return None
    penalty = int(violations.get("_time_penalty_seconds", 0))
    effective_limit = limit * 60 - penalty
    elapsed = _elapsed_seconds(attempt)
    return max(0, int(effective_limit - elapsed))


def _build_question_config(q_type: str, q: dict[str, Any]) -> dict[str, Any]:
    if q_type == "SINGLE_CHOICE":
        return {"options": q.get("options", []), "correct": int(q.get("correct", 0))}
    if q_type == "MULTIPLE_CHOICE":
        return {"options": q.get("options", []), "correct": [int(x) for x in q.get("correct", [])]}
    if q_type == "ORDERING":
        return {"items": q.get("items", []), "correct_order": [int(x) for x in q.get("correct_order", [])]}
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

        q_snap: dict[str, Any] = {
            "id": orig_idx,
            "type": q_type,
            "text": str(q.get("text", "")),
            "points": int(q.get("points", 1)),
            "is_required": bool(q.get("required", False)),
            "config": q_config,
        }

        if shuffle_opts and q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            options = list(q_config.get("options", []))
            indices = list(range(len(options)))
            random.shuffle(indices)
            shuffled_options = [options[i] for i in indices]
            if q_type == "SINGLE_CHOICE":
                original_correct = q_config.get("correct", 0)
                new_correct = indices.index(original_correct)
                q_snap["config"] = {"options": shuffled_options, "correct": new_correct}
            else:
                original_correct = set(q_config.get("correct", []))
                new_correct = [i for i, orig in enumerate(indices) if orig in original_correct]
                q_snap["config"] = {"options": shuffled_options, "correct": sorted(new_correct)}

        snapshot.append(q_snap)
    return snapshot


def _grade_answer(
    q_snap: dict[str, Any],
    raw_answer: Any,
) -> tuple[dict[str, Any], bool, int]:
    """Grade a single answer. Returns (answer_jsonb, is_correct, points_earned)."""
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
        try:
            selected = sorted(int(v) for v in values)
        except (ValueError, TypeError):
            selected = []
        is_correct = selected == sorted(q_config.get("correct", []))
        return {"selected": selected}, is_correct, q_points if is_correct else 0

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
        return {"text": text_answer}, None, 0  # type: ignore[return-value]

    return {"raw": str(raw_answer)}, False, 0


async def _count_used_attempts(db: DBSession, submission_id: int, exclude_id: int) -> int:  # type: ignore[valid-type]
    """Count finished (non-passing) attempts for a submission, excluding the given id."""
    result = await db.execute(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.submission_id == submission_id,
            QuizAttempt.status.in_([
                QuizAttemptStatus.COMPLETED,
                QuizAttemptStatus.TIMED_OUT,
                QuizAttemptStatus.VIOLATION_FAIL,
            ]),
            QuizAttempt.id != exclude_id,
        )
    )
    return result.scalar_one() or 0


async def _grade_and_finalize(
    attempt: QuizAttempt,
    db: DBSession,  # type: ignore[valid-type]
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
    if submission:
        if is_passed:
            submission.status = SubmissionStatus.COMPLETED
        else:
            max_attempts = attempt.config_snapshot.get("max_quiz_attempts")
            if max_attempts is not None:
                prior = await _count_used_attempts(db, attempt.submission_id, attempt.id)
                if prior + 1 >= max_attempts:
                    submission.status = SubmissionStatus.FAILED
                    attempts_left = 0
                else:
                    attempts_left = max_attempts - (prior + 1)
            # else: leave at QUIZ_SENT so student can retry

    db.add(OutboxMessage(
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
    ))

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

    quiz_cfg: dict[str, Any] = (
        config_record.config
        .get("assignments", {})
        .get(assignment_code, {})
        .get("quiz", {})
    )
    if not quiz_cfg or not quiz_cfg.get("questions"):
        raise HTTPException(status_code=404, detail="No quiz configured for this assignment")

    # Check max attempts
    max_attempts = quiz_cfg.get("max_quiz_attempts")
    used_count = sum(1 for a in existing if a.status in _TERMINAL_STATUSES)
    if max_attempts is not None and used_count >= max_attempts:
        latest_finished = existing[-1] if existing else None
        if latest_finished:
            return RedirectResponse(url=f"/portal/quiz/{latest_finished.id}/result", status_code=303)
        raise HTTPException(status_code=403, detail="No quiz attempts remaining")

    questions_snapshot = _build_questions_from_config(quiz_cfg)
    config_snapshot: dict[str, Any] = {
        "pass_threshold_pct": float(quiz_cfg.get("pass_threshold_pct", 0.6)),
        "show_correct_answers_after": bool(quiz_cfg.get("show_correct_answers_after", False)),
        "anti_cheat": quiz_cfg.get("anti_cheat", {}),
    }
    if max_attempts is not None:
        config_snapshot["max_quiz_attempts"] = int(max_attempts)
    if quiz_cfg.get("time_limit_minutes") is not None:
        config_snapshot["time_limit_minutes"] = int(quiz_cfg["time_limit_minutes"])

    attempt = QuizAttempt(
        submission_id=latest_sub.id,
        plugin_config_id=config_record.id,
        plugin_config_version=config_record.version,
        questions_snapshot=questions_snapshot,
        config_snapshot=config_snapshot,
        started_at=_utcnow(),
        status=QuizAttemptStatus.IN_PROGRESS,
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

    existing_answers = {a.question_id: a.answer for a in attempt.answers}
    seconds_remaining = _seconds_remaining(attempt)
    anti_cheat_config = attempt.config_snapshot.get("anti_cheat", {})

    return templates.TemplateResponse(
        request=request,
        name="student_quiz.html",
        context={
            "current_user": current_user,
            "attempt": attempt,
            "questions": attempt.questions_snapshot,
            "existing_answers": existing_answers,
            "seconds_remaining": seconds_remaining,
            "anti_cheat_config": anti_cheat_config,
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
                violations["_time_penalty_seconds"] = int(violations.get("_time_penalty_seconds", 0)) + penalty
                response_action = "reduce_time"
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

    return JSONResponse({
        "action": response_action,
        "seconds_remaining": seconds_remaining,
        "message": message,
        "violation_count": count,
    })


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
    attempt.answers = list(attempt_answers_result.scalars().all())  # type: ignore[assignment]

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
        question_results.append({
            "question": q_snap,
            "answer": ans.answer if ans else None,
            "is_correct": ans.is_correct if ans else None,
            "points_earned": ans.points_earned if ans else 0,
        })

    sa = submission.students_assignment
    sa_full_result = await db.execute(
        select(StudentAssignment)
        .where(StudentAssignment.id == sa.id)
        .options(selectinload(StudentAssignment.subjects_assignment))
    )
    sa_full = sa_full_result.scalar_one()
    subject_id = sa_full.subjects_assignment.subject_id

    return templates.TemplateResponse(
        request=request,
        name="student_quiz_result.html",
        context={
            "current_user": current_user,
            "attempt": attempt,
            "question_results": question_results,
            "show_correct": show_correct,
            "subject_id": subject_id,
            "student_assignment_id": sa.id,
        },
    )
