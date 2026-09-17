"""Authentication routes: login, logout, and password reset."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import JWTError
from sqlalchemy import select

from submissions_checker.api.dependencies import CurrentUser, DBSession
from submissions_checker.core import metrics
from submissions_checker.core.config import get_settings
from submissions_checker.core.i18n import get_vocab
from submissions_checker.core.rate_limit import (
    client_ip,
    get_login_ip_limiter,
    get_login_limiter,
)
from submissions_checker.core.security import (
    COOKIE_NAME,
    JWT_EXPIRY_HOURS,
    MAX_PASSWORD_BYTES,
    create_access_token,
    decode_access_token,
    dummy_password_hash,
    hash_password,
    password_too_long,
    verify_password,
)
from submissions_checker.core.templates import render
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.password_reset import PasswordResetToken
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.user import User
from submissions_checker.db.models.user_login import UserLogin
from submissions_checker.services.audit import audit
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import password_reset_template

router = APIRouter(prefix="/auth", tags=["auth"])

_MIN_PASSWORD_LEN = 8


def _auth_vocab(request: Request) -> dict[str, Any]:
    return get_vocab(request.cookies.get("lang")).get("auth", {})  # type: ignore[no-any-return]


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
    if role == UserRole.TEACHER:
        return "/teacher"
    if role == UserRole.ADMIN:
        return "/admin"
    return "/portal"


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
    limiter = get_login_limiter()
    ip_limiter = get_login_ip_limiter()
    ip = client_ip(request)
    throttle_key = f"{ip}|{username.strip().lower()}"
    if limiter.is_blocked(throttle_key) or ip_limiter.is_blocked(ip):
        return render(
            request,
            "login.html",
            {"current_user": None, "error": _auth_vocab(request).get("error_too_many_attempts")},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    result = await db.execute(
        select(User).where(User.username == username, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()

    # Always run one bcrypt check, so an unknown username costs the same time as a
    # wrong password. An over-long password is simply wrong (bcrypt caps at 72 bytes).
    hashed = user.password_hash if user is not None else dummy_password_hash()
    if not verify_password(password, hashed) or user is None:
        limiter.record_failure(throttle_key)
        ip_limiter.record_failure(ip)
        return render(
            request,
            "login.html",
            {"current_user": None, "error": "Invalid username or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    limiter.reset(throttle_key)
    db.add(UserLogin(user_id=user.id))
    await db.commit()
    metrics.logins_total.labels(role=user.role.value).inc()

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
    return render(
        request, "forgot_password.html", {"current_user": None, "sent": False, "error": None}
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(
    request: Request,
    db: DBSession,
    username: str = Form(...),
) -> HTMLResponse:
    # Every request costs budget: each one may send an email.
    limiter = get_login_limiter()
    throttle_key = f"forgot|{client_ip(request)}"
    if limiter.is_blocked(throttle_key):
        return render(
            request,
            "forgot_password.html",
            {
                "current_user": None,
                "sent": False,
                "error": _auth_vocab(request).get("error_too_many_attempts"),
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    limiter.record_failure(throttle_key)

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
        full_name = user.username
        if user.student_id:
            student = await db.get(Student, user.student_id)
            if student:
                email = student.email
                full_name = student.full_name

        if email:
            reset_url = f"{settings.app_base_url.rstrip('/')}/auth/reset-password?token={token_str}"
            subj, body = password_reset_template(full_name, reset_url)
            dispatcher = build_dispatcher(settings)
            if dispatcher._channels:
                await dispatcher.notify(email, subj, body)

    return render(
        request, "forgot_password.html", {"current_user": None, "sent": True, "error": None}
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: str,
    db: DBSession,
) -> HTMLResponse:
    result = await db.execute(select(PasswordResetToken).where(PasswordResetToken.token == token))
    prt = result.scalar_one_or_none()
    valid = prt is not None and prt.is_valid()
    return render(
        request,
        "reset_password.html",
        {"current_user": None, "token": token, "valid": valid, "error": None, "success": False},
    )


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password(
    request: Request,
    db: DBSession,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse:
    if new_password != confirm_password:
        return render(
            request,
            "reset_password.html",
            {
                "current_user": None,
                "token": token,
                "valid": True,
                "error": "Passwords do not match.",
                "success": False,
            },
            status_code=422,
        )

    if len(new_password) < 8 or password_too_long(new_password):
        return render(
            request,
            "reset_password.html",
            {
                "current_user": None,
                "token": token,
                "valid": True,
                "error": (f"Password must be between 8 characters and {MAX_PASSWORD_BYTES} bytes."),
                "success": False,
            },
            status_code=422,
        )

    result = await db.execute(select(PasswordResetToken).where(PasswordResetToken.token == token))
    prt = result.scalar_one_or_none()
    if prt is None or not prt.is_valid():
        return render(
            request,
            "reset_password.html",
            {"current_user": None, "token": token, "valid": False, "error": None, "success": False},
            status_code=400,
        )

    user = await db.get(User, prt.user_id)
    if user is None:
        raise HTTPException(status_code=404)

    user.password_hash = hash_password(new_password)
    prt.used = True
    await db.commit()

    return render(
        request,
        "reset_password.html",
        {"current_user": None, "token": token, "valid": True, "error": None, "success": True},
    )


# ── Change password (logged-in user) ─────────────────────────────────────────


def _render_change_password(
    request: Request,
    current_user: CurrentUser,
    *,
    error: str | None,
    changed: bool,
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "change_password.html",
        {"current_user": current_user, "error": error, "changed": changed},
        status_code=status_code,
    )


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: CurrentUser, changed: int = 0
) -> HTMLResponse:
    return _render_change_password(request, current_user, error=None, changed=bool(changed))


@router.post("/change-password", response_model=None)
async def change_password(
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """Let any signed-in account set its own password.

    Students receive generated credentials by email; without this the only way to
    pick a password was the forgot-password email round trip.
    """
    user = await db.get(User, current_user.user_id)
    if user is None:
        raise HTTPException(status_code=404)
    vocab = get_vocab(request.cookies.get("lang")).get("auth", {})
    error: str | None = None
    if not verify_password(current_password, user.password_hash):
        error = vocab.get("error_current_wrong", "Current password is wrong.")
    elif new_password != confirm_password:
        error = vocab.get("error_mismatch", "Passwords do not match.")
    elif len(new_password) < _MIN_PASSWORD_LEN:
        error = vocab.get("error_too_short", "Password must be at least 8 characters.")
    elif password_too_long(new_password):
        error = vocab.get("error_too_long", f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")
    elif new_password == current_password:
        error = vocab.get("error_same_as_current", "Choose a different password.")
    if error is not None:
        return _render_change_password(
            request, current_user, error=error, changed=False, status_code=422
        )

    user.password_hash = hash_password(new_password)
    await audit(
        db,
        action="change_password",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
    )
    await db.commit()
    return RedirectResponse(
        url="/auth/change-password?changed=1", status_code=status.HTTP_303_SEE_OTHER
    )
