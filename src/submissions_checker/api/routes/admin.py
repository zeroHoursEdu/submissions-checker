"""Admin portal routes — ADMIN role only."""

from __future__ import annotations

import secrets

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from submissions_checker.api.dependencies import AdminUser, DBSession
from submissions_checker.db.models import AuditLog, OutboxMessage, User
from submissions_checker.db.models.enums import OutboxMessageState, UserRole
from submissions_checker.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
) -> HTMLResponse:
    # System stats
    user_counts_result = await db.execute(
        select(User.role, func.count(User.id).label("cnt"))
        .group_by(User.role)
    )
    user_counts = {r.role: r.cnt for r in user_counts_result}

    outbox_result = await db.execute(
        select(OutboxMessage.state, func.count(OutboxMessage.id).label("cnt"))
        .group_by(OutboxMessage.state)
    )
    outbox_counts = {r.state: r.cnt for r in outbox_result}

    recent_audit_result = await db.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(20)
    )
    recent_audit = recent_audit_result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "current_user": current_user,
            "user_counts": user_counts,
            "outbox_counts": outbox_counts,
            "recent_audit": recent_audit,
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
) -> HTMLResponse:
    result = await db.execute(
        select(User).order_by(User.role, User.username)
    )
    users = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"current_user": current_user, "users": users},
    )


@router.get("/teachers/create", response_class=HTMLResponse)
async def create_teacher_page(
    request: Request,
    current_user: AdminUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="admin_create_teacher.html",
        context={"current_user": current_user, "error": None},
    )


@router.post("/teachers/create", response_model=None)
async def create_teacher(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    username = username.strip()
    if len(password) < 8:
        return templates.TemplateResponse(  # type: ignore[return-value]
            request=request,
            name="admin_create_teacher.html",
            context={"current_user": current_user, "error": "Password must be at least 8 characters."},
            status_code=422,
        )
    existing = await db.execute(select(User.id).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        return templates.TemplateResponse(  # type: ignore[return-value]
            request=request,
            name="admin_create_teacher.html",
            context={"current_user": current_user, "error": "Username already taken."},
            status_code=422,
        )

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    user = User(username=username, password_hash=password_hash, role=UserRole.TEACHER)
    db.add(user)
    await audit(
        db, action="create_teacher", actor_id=current_user.user_id,
        actor_username=current_user.username, new_username=username,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    db: DBSession,
    current_user: AdminUser,
) -> RedirectResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    if user.id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    user.is_active = not user.is_active
    await audit(
        db, action="toggle_user_active", actor_id=current_user.user_id,
        actor_username=current_user.username, target_user_id=user_id, is_active=user.is_active,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
) -> HTMLResponse:
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)
    )
    logs = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin_audit.html",
        context={"current_user": current_user, "logs": logs},
    )
