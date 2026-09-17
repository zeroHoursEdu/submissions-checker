"""Student-side squad formation: invite classmates, answer invites."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from submissions_checker.api.dependencies import DBSession, StudentId, StudentUser
from submissions_checker.db.models import SubjectsStudents
from submissions_checker.services import squads
from submissions_checker.services.squads import SquadError

router = APIRouter(prefix="/portal/subjects/{subject_id}/squad", tags=["student-squads"])


def _back(
    subject_id: int, *, error: str | None = None, flash: str | None = None
) -> RedirectResponse:
    url = f"/portal/subjects/{subject_id}"
    if error:
        url += f"?squad_error={error}"
    elif flash:
        url += f"?squad_flash={flash}"
    return RedirectResponse(url=url, status_code=303)


async def _require_enrolled(db: DBSession, subject_id: int, student_id: int) -> None:
    row = await db.scalar(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == subject_id, SubjectsStudents.student_id == student_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not enrolled in this subject")


@router.post("/create")
async def create_squad(
    subject_id: int,
    request: Request,
    db: DBSession,
    current_user: StudentUser,
    student_id: StudentId,
) -> RedirectResponse:
    await _require_enrolled(db, subject_id, student_id)
    form = await request.form()
    raw_ids = form.getlist("invitee_ids")
    invitee_ids = [int(v) for v in raw_ids if isinstance(v, str) and v.isdigit()]
    try:
        await squads.create_with_invites(db, subject_id, student_id, invitee_ids)
    except SquadError as exc:
        await db.rollback()
        return _back(subject_id, error=exc.reason)
    await db.commit()
    return _back(subject_id, flash="invited")


async def _answer(
    db: DBSession, subject_id: int, student_id: int, invite_id: int, verb: str
) -> RedirectResponse:
    await _require_enrolled(db, subject_id, student_id)
    try:
        if verb == "accept":
            await squads.accept_invite(db, invite_id, student_id)
            flash = "joined"
        elif verb == "decline":
            await squads.decline_invite(db, invite_id, student_id)
            flash = "declined"
        else:
            await squads.cancel_invite(db, invite_id, student_id)
            flash = "cancelled"
    except SquadError as exc:
        # An accept refused for eligibility has already flipped the invite to CANCELLED;
        # keep that write.
        await db.commit()
        return _back(subject_id, error=exc.reason)
    await db.commit()
    return _back(subject_id, flash=flash)


@router.post("/invites/{invite_id}/accept")
async def accept(
    subject_id: int, invite_id: int, db: DBSession, current_user: StudentUser, student_id: StudentId
) -> RedirectResponse:
    return await _answer(db, subject_id, student_id, invite_id, "accept")


@router.post("/invites/{invite_id}/decline")
async def decline(
    subject_id: int, invite_id: int, db: DBSession, current_user: StudentUser, student_id: StudentId
) -> RedirectResponse:
    return await _answer(db, subject_id, student_id, invite_id, "decline")


@router.post("/invites/{invite_id}/cancel")
async def cancel(
    subject_id: int, invite_id: int, db: DBSession, current_user: StudentUser, student_id: StudentId
) -> RedirectResponse:
    return await _answer(db, subject_id, student_id, invite_id, "cancel")
