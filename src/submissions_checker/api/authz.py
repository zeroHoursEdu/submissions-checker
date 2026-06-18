"""Object-level authorization helpers."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.api.dependencies import CurrentUserData
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.subject import Subject


async def require_subject_access(
    db: AsyncSession, subject_id: int, current_user: CurrentUserData
) -> Subject:
    """Return the Subject if current_user owns it or is ADMIN; else raise 403/404."""
    subject = (
        await db.execute(select(Subject).where(Subject.id == subject_id))
    ).scalar_one_or_none()
    if subject is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found"
        )
    if current_user.role != UserRole.ADMIN and subject.owner_id != current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this subject",
        )
    return subject
