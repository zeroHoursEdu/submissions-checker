"""Authentication routes: login, logout, and password reset."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError
from sqlalchemy import select

from submissions_checker.api.dependencies import DBSession
from submissions_checker.core.config import get_settings
from submissions_checker.core.security import (
    COOKIE_NAME,
    JWT_EXPIRY_HOURS,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.password_reset import PasswordResetToken
from submissions_checker.db.models.user import User
from submissions_checker.db.models.user_login import UserLogin
from submissions_checker.db.models.student import Student
from submissions_checker.core.templates import render
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import password_reset_template

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=get_settings().cookie_secure,
        max_age=JWT_EXPIRY_HOURS * 3600,
    )


def _redirect_by_role(role: UserRole) -> str:
    return "/teacher" if role == UserRole.TEACHER else "/portal"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = decode_access_token(token)
            role = UserRole(payload["role"])
            return RedirectResponse(url=_redirect_by_role(role), status_code=302)  # type: ignore[return-value]
        except (JWTError, KeyError, ValueError):
            pass
    return render(request, "login.html", {"current_user": None, "error": None})


@router.post("/login")
async def login(
    request: Request,
    db: DBSession,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    result = await db.execute(
        select(User).where(User.username == username, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"current_user": None, "error": "Invalid username or password"}, status_code=status.HTTP_401_UNAUTHORIZED)

    db.add(UserLogin(user_id=user.id))
    await db.commit()

    token = create_access_token(user.id, user.username, user.role.value)
    redirect_url = _redirect_by_role(user.role)
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=COOKIE_NAME, httponly=True, samesite="strict")
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request) -> HTMLResponse:
    return render(request, "forgot_password.html", {"current_user": None, "sent": False, "error": None})


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(
    request: Request,
    db: DBSession,
    username: str = Form(...),
) -> HTMLResponse:
    settings = get_settings()
    result = await db.execute(
        select(User).where(User.username == username.strip(), User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()

    # Always return success to avoid username enumeration
    if user is not None:
        token_str = secrets.token_urlsafe(64)
        reset_token = PasswordResetToken.create(user_id=user.id, token=token_str)
        db.add(reset_token)
        await db.commit()

        # Resolve email address
        email: str | None = None
        if user.student_id:
            student = await db.get(Student, user.student_id)
            if student:
                email = student.email

        if email:
            reset_url = f"{settings.app_base_url.rstrip('/')}/auth/reset-password?token={token_str}"
            full_name = student.full_name if user.student_id else user.username  # type: ignore[possibly-undefined]
            subj, body = password_reset_template(full_name, reset_url)
            dispatcher = build_dispatcher(settings)
            if dispatcher._channels:
                await dispatcher.notify(email, subj, body)

    return render(request, "forgot_password.html", {"current_user": None, "sent": True, "error": None})


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: str,
    db: DBSession,
) -> HTMLResponse:
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token == token)
    )
    prt = result.scalar_one_or_none()
    valid = prt is not None and prt.is_valid()
    return render(request, "reset_password.html", {"current_user": None, "token": token, "valid": valid, "error": None, "success": False})


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password(
    request: Request,
    db: DBSession,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse:
    if new_password != confirm_password:
        return render(request, "reset_password.html", {"current_user": None, "token": token, "valid": True, "error": "Passwords do not match.", "success": False}, status_code=422)

    if len(new_password) < 8:
        return render(request, "reset_password.html", {"current_user": None, "token": token, "valid": True, "error": "Password must be at least 8 characters.", "success": False}, status_code=422)

    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token == token)
    )
    prt = result.scalar_one_or_none()
    if prt is None or not prt.is_valid():
        return render(request, "reset_password.html", {"current_user": None, "token": token, "valid": False, "error": None, "success": False}, status_code=400)

    user = await db.get(User, prt.user_id)
    if user is None:
        raise HTTPException(status_code=404)

    user.password_hash = hash_password(new_password)
    prt.used = True
    await db.commit()

    return render(request, "reset_password.html", {"current_user": None, "token": token, "valid": True, "error": None, "success": True})
