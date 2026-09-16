"""Teacher panel for ruling on reported quiz questions.

A student's report arrives as a notification-bell row linking straight here. The teacher
sees the question exactly as that student saw it, must write a note saying why, and either
accepts (the question is credited to everyone who drew it, and their grades follow) or
rejects (the question stands).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.api.authz import require_subject_access
from submissions_checker.api.dependencies import CurrentUserData, DBSession, TeacherUser
from submissions_checker.core import metrics
from submissions_checker.core.logging import get_logger
from submissions_checker.core.templates import render
from submissions_checker.db.models.enums import QuizDisputeStatus, UserRole
from submissions_checker.db.models.quiz_dispute import QuizQuestionDispute
from submissions_checker.db.models.quiz_template import QuizAttempt
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.services.audit import audit
from submissions_checker.services.quiz_regrade import resolve_dispute

logger = get_logger(__name__)

router = APIRouter(prefix="/teacher", tags=["teacher-disputes"])

_VALID_ACTIONS = ("accept", "reject")


def _dispute_scope_query() -> Any:
    """Join a dispute down to the subject that owns it, for scoping and authz."""
    return (
        select(QuizQuestionDispute, Student, SubjectsAssignment, Subject.id.label("subject_id"))
        .join(QuizAttempt, QuizQuestionDispute.attempt_id == QuizAttempt.id)
        .join(Submission, QuizAttempt.submission_id == Submission.id)
        .join(StudentAssignment, Submission.students_assignment_id == StudentAssignment.id)
        .join(
            SubjectsAssignment,
            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
        )
        .join(Subject, SubjectsAssignment.subject_id == Subject.id)
        .join(Student, QuizQuestionDispute.student_id == Student.id)
    )


async def _subject_id_for_dispute(db: DBSession, dispute_id: int) -> int | None:
    subject_id: int | None = await db.scalar(
        select(SubjectsAssignment.subject_id)
        .select_from(QuizQuestionDispute)
        .join(QuizAttempt, QuizQuestionDispute.attempt_id == QuizAttempt.id)
        .join(Submission, QuizAttempt.submission_id == Submission.id)
        .join(StudentAssignment, Submission.students_assignment_id == StudentAssignment.id)
        .join(
            SubjectsAssignment,
            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
        )
        .where(QuizQuestionDispute.id == dispute_id)
    )
    return subject_id


def _scope_to_teacher(query: Any, current_user: CurrentUserData) -> Any:
    """Restrict a dispute query to the subjects this teacher owns. Admins see everything."""
    if current_user.role == UserRole.ADMIN:
        return query
    return query.where(Subject.owner_id == current_user.user_id)


async def count_open_disputes(db: AsyncSession, current_user: CurrentUserData) -> int:
    """Open reports this teacher is responsible for — the dashboard badge."""
    query = _scope_to_teacher(
        select(func.count(QuizQuestionDispute.id))
        .select_from(QuizQuestionDispute)
        .join(QuizAttempt, QuizQuestionDispute.attempt_id == QuizAttempt.id)
        .join(Submission, QuizAttempt.submission_id == Submission.id)
        .join(StudentAssignment, Submission.students_assignment_id == StudentAssignment.id)
        .join(
            SubjectsAssignment,
            StudentAssignment.subjects_assignment_id == SubjectsAssignment.id,
        )
        .join(Subject, SubjectsAssignment.subject_id == Subject.id)
        .where(QuizQuestionDispute.status == QuizDisputeStatus.OPEN),
        current_user,
    )
    return int(await db.scalar(query) or 0)


def _render_answer(q_snap: dict[str, Any], answer: Any) -> str:
    """The student's answer, spelled out against the options THEY were shown."""
    if answer is None:
        return "—"
    payload = answer.answer or {}
    q_type = q_snap.get("type")
    config = q_snap.get("config") or {}
    options: list[str] = config.get("options", [])

    if q_type == "SINGLE_CHOICE":
        idx = payload.get("selected")
        if idx is None or not (0 <= int(idx) < len(options)):
            return "—"
        return str(options[int(idx)])
    if q_type == "MULTIPLE_CHOICE":
        picked = [int(i) for i in payload.get("selected", [])]
        return ", ".join(str(options[i]) for i in picked if 0 <= i < len(options)) or "—"
    if q_type == "ORDERING":
        items: list[str] = config.get("items", [])
        order = [int(i) for i in payload.get("order", [])]
        return " → ".join(str(items[i]) for i in order if 0 <= i < len(items)) or "—"
    if q_type == "TRUE_FALSE":
        value = payload.get("value")
        return "—" if value is None else ("True" if value else "False")
    return str(payload) or "—"


@router.get("/disputes", response_class=HTMLResponse)
async def list_disputes(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    """Open reports across the subjects this teacher owns (all of them, for an admin)."""
    query = _scope_to_teacher(
        _dispute_scope_query().where(QuizQuestionDispute.status == QuizDisputeStatus.OPEN),
        current_user,
    ).order_by(QuizQuestionDispute.created_at.desc())

    rows = (await db.execute(query)).all()

    attempt_ids = {row[0].attempt_id for row in rows}
    snapshots: dict[int, list[dict[str, Any]]] = {}
    if attempt_ids:
        for attempt_id, snapshot in (
            await db.execute(
                select(QuizAttempt.id, QuizAttempt.questions_snapshot).where(
                    QuizAttempt.id.in_(attempt_ids)
                )
            )
        ).all():
            snapshots[attempt_id] = snapshot or []

    items = []
    for dispute, student, subjects_assignment, _subject_id in rows:
        q_snap = next(
            (
                q
                for q in snapshots.get(dispute.attempt_id, [])
                if q.get("id") == dispute.question_id
            ),
            None,
        )
        items.append(
            {
                "id": dispute.id,
                "student_name": student.full_name,
                "assignment_title": subjects_assignment.title,
                "question_text": (q_snap or {}).get("text", f"Question #{dispute.question_id}"),
                "student_note": dispute.student_note,
                "created_at": dispute.created_at,
            }
        )

    return render(
        request,
        "teacher_disputes.html",
        {"current_user": current_user, "disputes": items},
    )


@router.get("/disputes/{dispute_id}", response_class=HTMLResponse)
async def dispute_detail(
    dispute_id: int,
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    """The ruling panel for one report."""
    row = (
        await db.execute(_dispute_scope_query().where(QuizQuestionDispute.id == dispute_id))
    ).first()
    if row is None:
        raise HTTPException(status_code=404)
    dispute, student, subjects_assignment, subject_id = row
    await require_subject_access(db, subject_id, current_user)

    attempt = await db.get(
        QuizAttempt, dispute.attempt_id, options=[selectinload(QuizAttempt.answers)]
    )
    if attempt is None:
        raise HTTPException(status_code=404)

    # From the attempt's OWN snapshot: options are shuffled per attempt, so this is the
    # only key against which the student's stored answer index means anything.
    q_snap = next(
        (q for q in (attempt.questions_snapshot or []) if q.get("id") == dispute.question_id),
        None,
    )
    if q_snap is None:
        raise HTTPException(status_code=404)
    answer = next((a for a in attempt.answers if a.question_id == dispute.question_id), None)

    correct = (q_snap.get("config") or {}).get("correct")
    correct_indexes = (
        [int(i) for i in correct]
        if isinstance(correct, list)
        else ([int(correct)] if isinstance(correct, int) else [])
    )

    # How many other finished attempts an accept would re-score, so the teacher can see
    # the blast radius before deciding.
    affected_count = 0
    if dispute.plugin_config_id is not None:
        affected_count = int(
            await db.scalar(
                select(func.count(QuizAttempt.id)).where(
                    QuizAttempt.plugin_config_id == dispute.plugin_config_id,
                    QuizAttempt.plugin_config_version == dispute.plugin_config_version,
                    QuizAttempt.is_passed.is_not(True),
                )
            )
            or 0
        )

    return render(
        request,
        "teacher_dispute_detail.html",
        {
            "current_user": current_user,
            "dispute": dispute,
            "student": student,
            "assignment_title": subjects_assignment.title,
            "attempt": attempt,
            "question": q_snap,
            "options": (q_snap.get("config") or {}).get("options", []),
            "correct_indexes": correct_indexes,
            "student_answer": _render_answer(q_snap, answer),
            "answer_is_correct": answer.is_correct if answer else None,
            "affected_count": affected_count,
            "config_version": dispute.plugin_config_version,
            "is_open": dispute.status == QuizDisputeStatus.OPEN,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/disputes/{dispute_id}/resolve")
async def resolve(
    dispute_id: int,
    db: DBSession,
    current_user: TeacherUser,
    action: str = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    """Accept or reject a report. A note is mandatory either way."""
    subject_id = await _subject_id_for_dispute(db, dispute_id)
    if subject_id is None:
        raise HTTPException(status_code=404)
    await require_subject_access(db, subject_id, current_user)

    dispute = await db.get(QuizQuestionDispute, dispute_id)
    if dispute is None:
        raise HTTPException(status_code=404)

    if action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail="action must be accept or reject")

    note = note.strip()
    if not note:
        # The note is the whole point of the panel: a ruling with no reason is not a ruling,
        # and the student is told this text verbatim.
        raise HTTPException(status_code=400, detail="A note explaining the decision is required")

    if dispute.status != QuizDisputeStatus.OPEN:
        raise HTTPException(status_code=409, detail="This report has already been resolved")

    outcome = await resolve_dispute(
        db,
        dispute,
        accept=action == "accept",
        note=note,
        resolved_by_user_id=current_user.user_id,
    )

    await audit(
        db,
        action=f"teacher_{action}_question_dispute",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="quiz_question_dispute",
        target_id=dispute_id,
        question_id=dispute.question_id,
        reason=note,
        affected_attempts=len(outcome.rescored),
        also_resolved=len(outcome.resolved_dispute_ids) - 1,
    )
    await db.commit()
    metrics.disputes_resolved_total.labels(status=dispute.status.value).inc()

    return RedirectResponse(url="/teacher/disputes", status_code=303)
