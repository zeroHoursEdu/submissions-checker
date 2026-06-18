"""Dependency injection helpers for FastAPI routes."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import Settings, get_settings
from submissions_checker.core.database import get_db
from submissions_checker.core.security import COOKIE_NAME, decode_access_token
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.user import User

# Type aliases for common dependencies
DBSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@dataclass
class CurrentUserData:
    user_id: int
    username: str
    role: UserRole


async def _get_current_user(
    access_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> CurrentUserData:
    """Extract and validate JWT from HTTP-only cookie, then verify the user against the DB.

    Raises 401 if the cookie is absent/invalid, or if the user no longer exists or is inactive.
    """
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_access_token(access_token)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    user_id = int(payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    return CurrentUserData(
        user_id=user.id,
        username=user.username,
        role=user.role,
    )


async def _require_teacher(
    current_user: CurrentUserData = Depends(_get_current_user),
) -> CurrentUserData:
    if current_user.role not in (UserRole.TEACHER, UserRole.ADMIN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Teacher access required")
    return current_user


async def _require_student(
    current_user: CurrentUserData = Depends(_get_current_user),
) -> CurrentUserData:
    if current_user.role != UserRole.STUDENT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Student access required")
    return current_user


async def _require_admin(
    current_user: CurrentUserData = Depends(_get_current_user),
) -> CurrentUserData:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


async def _get_student_id_for_user(
    current_user: CurrentUserData = Depends(_require_student),
    db: AsyncSession = Depends(get_db),
) -> int:
    result = await db.execute(select(User.student_id).where(User.id == current_user.user_id))
    student_id = result.scalar_one_or_none()
    if student_id is None:
        raise HTTPException(status_code=404, detail="No student record linked to this account")
    return student_id


CurrentUser = Annotated[CurrentUserData, Depends(_get_current_user)]
TeacherUser = Annotated[CurrentUserData, Depends(_require_teacher)]
StudentUser = Annotated[CurrentUserData, Depends(_require_student)]
AdminUser = Annotated[CurrentUserData, Depends(_require_admin)]
StudentId = Annotated[int, Depends(_get_student_id_for_user)]
