"""Per-student LLM grading state for the assignment board and the approval gate.

A fixed number of queries for the whole board (no per-row lookups). Squad-aware: a
student's state covers the works of every member of their squad, so the draft one member
submitted through Classroom shows on all members' rows, just like the shared points.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models import StudentAssignment, SubjectsAssignment
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import ClassroomLinkMethod, LLMGradingStatus
from submissions_checker.db.models.squad import SquadMember
from submissions_checker.services.google.links import link_state_for_students
from submissions_checker.services.llm_grading.config import llm_criteria


async def _squad_groups(
    db: AsyncSession, subject_id: int, student_ids: Iterable[int]
) -> dict[int, set[int]]:
    """student_id -> the student ids whose works count for them (self + squad-mates)."""
    ids = set(student_ids)
    groups: dict[int, set[int]] = {sid: {sid} for sid in ids}
    if not ids:
        return groups
    mine = (
        select(SquadMember.squad_id)
        .where(SquadMember.subject_id == subject_id, SquadMember.student_id.in_(ids))
        .scalar_subquery()
    )
    by_squad: dict[int, set[int]] = {}
    for squad_id, student_id in await db.execute(
        select(SquadMember.squad_id, SquadMember.student_id).where(
            SquadMember.subject_id == subject_id, SquadMember.squad_id.in_(mine)
        )
    ):
        by_squad.setdefault(squad_id, set()).add(student_id)
    for members in by_squad.values():
        for sid in members & ids:
            groups[sid] = set(members)
    return groups


def _prefill(draft: dict[str, Any] | None, keys: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    crit = (draft or {}).get("criteria") or {}
    for key in keys:
        points = (crit.get(key) or {}).get("points")
        if isinstance(points, int | float):
            out[key] = int(points)
    return out


async def llm_board_state(
    db: AsyncSession, assignment: SubjectsAssignment, student_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Per student: link, latest work + its grading, latest DONE draft, approval state.

    "Latest" orders works by ``seen_at desc, id desc`` — the same order ingest uses.
    """
    subject_id = assignment.subject_id
    links = await link_state_for_students(db, subject_id)
    groups = await _squad_groups(db, subject_id, student_ids)
    everyone = set().union(*groups.values()) if groups else set()

    works: list[tuple[ClassroomWork, int]] = []
    if everyone:
        works = [
            (w, sid)
            for w, sid in await db.execute(
                select(ClassroomWork, ClassroomStudentLink.student_id)
                .join(ClassroomStudentLink, ClassroomStudentLink.id == ClassroomWork.link_id)
                .where(
                    ClassroomWork.subjects_assignment_id == assignment.id,
                    ClassroomStudentLink.student_id.in_(everyone),
                )
                .order_by(ClassroomWork.seen_at.desc(), ClassroomWork.id.desc())
            )
        ]
    gradings: dict[int, LLMGrading] = {}
    if works:
        gradings = {
            g.classroom_work_id: g
            for g in (
                await db.execute(
                    select(LLMGrading).where(
                        LLMGrading.classroom_work_id.in_([w.id for w, _ in works])
                    )
                )
            ).scalars()
        }
    scored_ids: set[int] = set()
    if student_ids:
        scored_ids = {
            sid
            for sid, scores in await db.execute(
                select(StudentAssignment.student_id, StudentAssignment.teacher_scores).where(
                    StudentAssignment.subjects_assignment_id == assignment.id,
                    StudentAssignment.student_id.in_(student_ids),
                )
            )
            if scores
        }
    keys = [c.key for c in llm_criteria((assignment.config or {}).get("grading"))]

    state: dict[int, dict[str, Any]] = {}
    for sid in student_ids:
        group = groups.get(sid, {sid})
        mine = [w for w, owner in works if owner in group]  # already newest first
        work = mine[0] if mine else None
        grading = gradings.get(work.id) if work else None
        done = next(
            (
                g
                for w in mine
                if (g := gradings.get(w.id)) is not None and g.status == LLMGradingStatus.DONE
            ),
            None,
        )
        approvals = [
            (g.approved_at, g)
            for w in mine
            if (g := gradings.get(w.id)) is not None and g.approved_at is not None
        ]
        approved = max(approvals, key=lambda pair: pair[0])[1] if approvals else None
        draft_id = done.id if done else None
        approved_id = approved.id if approved else None
        link = links.get(sid)
        state[sid] = {
            "link": link,
            "work": work,
            "grading": grading,
            "draft": done.draft if done else None,
            "draft_grading_id": draft_id,
            "approved_grading_id": approved_id,
            "needs_review": bool(draft_id and approved_id is not None and draft_id != approved_id),
            "needs_link_confirm": bool(
                link and link.method == ClassroomLinkMethod.NAME and not link.confirmed
            ),
            "prefill": {} if sid in scored_ids else _prefill(done.draft if done else None, keys),
        }
    return state
