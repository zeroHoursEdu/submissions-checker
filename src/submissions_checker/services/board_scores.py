"""Saving one row of the assignment board: the teacher's scores and checks for a student.

Shared by the per-row route and the board's batch save. A row either saves whole or raises
``RowError`` (never an HTTPException) so the batch can report it and keep the other rows.

Lock order, as everywhere settle runs: the scope's StudentAssignment rows first
(``settle.lock_scope``), then the latest submission (settle itself). Nothing here locks the
submission before settle.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.i18n import get_vocab
from submissions_checker.db.models import (
    OutboxMessage,
    StudentAssignment,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
)
from submissions_checker.db.models.classroom import LLMGrading
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    SubmissionStatus,
)
from submissions_checker.services import requirements, settle, squads
from submissions_checker.services.audit import audit
from submissions_checker.services.llm_grading import config as llm_config
from submissions_checker.services.llm_grading.board import llm_board_state

ROW_ERROR_CODES = frozenset(
    {"not_enrolled", "confirm_student", "draft_changed", "invalid", "completed_required"}
)


class RowError(Exception):
    """One board row could not be saved. ``code`` is one of ``ROW_ERROR_CODES``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class Actor(Protocol):
    user_id: int
    username: str


def board_values(plan: requirements.Plan, scores: Mapping[str, int]) -> dict[str, str]:
    """A row's teacher items as the board renders them: score -> its number or "",
    check -> "1" when saved, else ""."""
    out: dict[str, str] = {}
    for item in plan.teacher_items():
        value = scores.get(item.key)
        if item.type == "check":
            out[item.key] = "1" if value == 1 else ""
        else:
            out[item.key] = "" if value is None else str(value)
    return out


def posted_values(
    plan: requirements.Plan, row: Mapping[str, str], saved: Mapping[str, int]
) -> dict[str, str]:
    """A posted row in ``board_values`` terms, so its signature matches the rendered one
    when nothing was touched. A saved check renders no input (it cannot be unticked), so
    it posts nothing yet reads as "1"; a ticked box posts "on" or "1"."""
    out: dict[str, str] = {}
    for item in plan.teacher_items():
        raw = str(row.get(item.key) or "").strip()
        if item.type == "check":
            out[item.key] = "1" if raw in ("1", "on") or saved.get(item.key) == 1 else ""
        else:
            out[item.key] = raw
    return out


def _parse_id(raw: str | None) -> int | None:
    posted = str(raw or "").strip()
    # isascii+isdecimal, not isdigit: "²".isdigit() is True and int() would raise.
    return int(posted) if posted.isascii() and posted.isdecimal() else None


async def save_row(
    db: AsyncSession,
    assignment: SubjectsAssignment,
    student_id: int,
    form: Mapping[str, str],
    *,
    prefix: str,
    llm_grading_id: str | None,
    actor: Actor,
) -> settle.SettleResult:
    """Store one student's teacher items and settle their work. Does not commit.

    Squad points are shared like the grade. LLM-graded: saving approves the draft the row
    showed, refused when the student's name match is unconfirmed or a newer draft landed.
    """
    subject_id, sa_id = assignment.subject_id, assignment.id
    plan = settle.plan_for(assignment)
    enrolled = await db.scalar(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == subject_id,
            SubjectsStudents.student_id == student_id,
        )
    )
    if enrolled is None:
        raise RowError("not_enrolled", "Student is not enrolled")

    shown: dict[str, Any] | None = None
    if llm_config.is_llm_graded(assignment.config):
        shown = (await llm_board_state(db, assignment, [student_id]))[student_id]
        vocab = get_vocab(None).get("classroom", {})
        if shown["needs_link_confirm"]:
            # A name match may be the wrong student: points must not land on them unchecked.
            raise RowError(
                "confirm_student",
                str(vocab.get("confirm_student_first", "")) or "confirm student first",
            )
        if shown["draft_grading_id"] != _parse_id(llm_grading_id):
            # A newer draft landed after the page was rendered: never approve it unseen.
            raise RowError(
                "draft_changed", str(vocab.get("draft_changed", "")) or "draft changed, reload"
            )

    own = await db.scalar(
        select(StudentAssignment).where(
            StudentAssignment.student_id == student_id,
            StudentAssignment.subjects_assignment_id == sa_id,
        )
    )
    if own is None:
        own = StudentAssignment(student_id=student_id, subjects_assignment_id=sa_id)
        db.add(own)
        await db.flush()
    # Scope rows first (settle's order), then read the saved items: a check saved by a
    # concurrent save is then seen, and the merge below keeps it.
    await settle.lock_scope(db, own, assignment)
    own_id = own.id
    old = dict(own.teacher_scores or {})
    try:
        new_scores = requirements.parse_scores_form(plan, form, prefix=prefix, locked=old)
    except requirements.ScoreError as exc:
        raise RowError("invalid", str(exc)) from exc

    submission = await squads.latest_submission(db, own)
    status = (
        await db.scalar(select(Submission.status).where(Submission.id == submission.id))
        if submission is not None
        else None
    )
    if status == SubmissionStatus.COMPLETED:
        lost = [
            i.key
            for i in plan.teacher_items()
            if not i.optional and i.key in old and i.key not in new_scores
        ]
        if lost:
            raise RowError("completed_required", "A graded work cannot lose a required score")

    squad = await squads.squad_of(db, subject_id, student_id)
    target_ids = await squads.member_sa_ids(db, squad, sa_id) if squad else [own_id]
    for row_id in target_ids:
        row = await db.get(StudentAssignment, row_id)
        if row is not None:
            row.teacher_scores = dict(new_scores)

    approval: dict[str, Any] = {}
    if shown is not None and shown["draft_grading_id"] is not None:
        grading = await db.get(LLMGrading, shown["draft_grading_id"])
        if grading is not None:
            grading.approved_by = actor.user_id
            grading.approved_at = datetime.now(UTC)
            draft_points = {
                k: (v or {}).get("points")
                for k, v in ((shown["draft"] or {}).get("criteria") or {}).items()
            }
            approval = {
                "llm_grading_id": grading.id,
                "edited_from_draft": any(new_scores.get(k) != p for k, p in draft_points.items()),
            }
    await audit(
        db,
        action="teacher_scores_set",
        actor_id=actor.user_id,
        actor_username=actor.username,
        target_type="student_assignment",
        target_id=own_id,
        student_id=student_id,
        old=old,
        new=new_scores,
        **approval,
    )

    await db.flush()
    result = await settle.settle(db, own)
    if result.completed_now and result.submission_id is not None:
        db.add(
            OutboxMessage(
                event_type=OutboxEventType.SUBMISSION_REVIEWED,
                state=OutboxMessageState.PENDING,
                payload={"submission_id": result.submission_id, "action": "approve", "reason": ""},
            )
        )
    return result
