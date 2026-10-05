"""Queries over a subject's Classroom student links."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.classroom import ClassroomStudentLink


async def link_state_for_students(
    db: AsyncSession, subject_id: int
) -> dict[int, ClassroomStudentLink]:
    """Map platform student_id -> its Classroom link in this subject.

    If a student somehow has several links, a confirmed one wins, then the newest.
    """
    links = (
        (
            await db.execute(
                select(ClassroomStudentLink)
                .where(
                    ClassroomStudentLink.subject_id == subject_id,
                    ClassroomStudentLink.student_id.is_not(None),
                )
                .order_by(ClassroomStudentLink.confirmed.asc(), ClassroomStudentLink.id.asc())
            )
        )
        .scalars()
        .all()
    )
    # Ascending (unconfirmed first, oldest first): later entries overwrite, so the
    # confirmed, newest link ends up in the dict.
    return {link.student_id: link for link in links if link.student_id is not None}
