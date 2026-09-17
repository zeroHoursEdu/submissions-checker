"""Admin portal routes — ADMIN role only."""

from __future__ import annotations

from datetime import date

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.api.dependencies import AdminUser, DBSession
from submissions_checker.core.security import MAX_PASSWORD_BYTES, password_too_long
from submissions_checker.core.templates import render
from submissions_checker.db.models import AuditLog, OutboxMessage, Semester, User
from submissions_checker.db.models.enums import UserRole
from submissions_checker.services.audit import audit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
) -> HTMLResponse:
    # System stats
    user_counts_result = await db.execute(
        select(User.role, func.count(User.id).label("cnt")).group_by(User.role)
    )
    user_counts = {r.role: r.cnt for r in user_counts_result}

    outbox_result = await db.execute(
        select(OutboxMessage.state, func.count(OutboxMessage.id).label("cnt")).group_by(
            OutboxMessage.state
        )
    )
    outbox_counts = {r.state: r.cnt for r in outbox_result}

    recent_audit_result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)
    )
    recent_audit = recent_audit_result.scalars().all()

    return render(
        request,
        "admin_dashboard.html",
        {
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
    result = await db.execute(select(User).order_by(User.role, User.username))
    users = result.scalars().all()
    return render(request, "admin_users.html", {"current_user": current_user, "users": users})


@router.get("/teachers/create", response_class=HTMLResponse)
async def create_teacher_page(
    request: Request,
    current_user: AdminUser,
) -> HTMLResponse:
    return render(
        request, "admin_create_teacher.html", {"current_user": current_user, "error": None}
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
    if len(password) < 8 or password_too_long(password):
        return render(
            request,
            "admin_create_teacher.html",
            {
                "current_user": current_user,
                "error": f"Password must be between 8 characters and {MAX_PASSWORD_BYTES} bytes.",
            },
            status_code=422,
        )
    existing = await db.execute(select(User.id).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        return render(
            request,
            "admin_create_teacher.html",
            {"current_user": current_user, "error": "Username already taken."},
            status_code=422,
        )

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    user = User(username=username, password_hash=password_hash, role=UserRole.TEACHER)
    db.add(user)
    await audit(
        db,
        action="create_teacher",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        new_username=username,
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
        db,
        action="toggle_user_active",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_user_id=user_id,
        is_active=user.is_active,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
) -> HTMLResponse:
    result = await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200))
    logs = result.scalars().all()
    return render(request, "admin_audit.html", {"current_user": current_user, "logs": logs})


# ── Semesters ─────────────────────────────────────────────────────────────────

_SEASONS = ("SPRING", "FALL")


def _parse_semester_form(
    name: str, season: str, start_date: str, end_date: str
) -> tuple[str, str, date, date] | str:
    """Return the parsed fields, or an error code the template maps to a message."""
    name = name.strip()
    season = season.strip().upper()
    if not name or season not in _SEASONS:
        return "invalid"
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        return "invalid"
    if end <= start:
        return "inverted"
    return name, season, start, end


async def _semester_overlaps(
    db: AsyncSession, start: date, end: date, *, exclude_id: int | None
) -> bool:
    q = select(Semester.id).where(Semester.start_date <= end, Semester.end_date >= start)
    if exclude_id is not None:
        q = q.where(Semester.id != exclude_id)
    return (await db.execute(q)).first() is not None


async def _render_semesters(
    request: Request,
    db: AsyncSession,
    current_user: AdminUser,
    *,
    error: str | None,
    status_code: int = 200,
) -> HTMLResponse:
    rows = (await db.execute(select(Semester).order_by(Semester.start_date.desc()))).scalars().all()
    return render(
        request,
        "admin_semesters.html",
        {
            "current_user": current_user,
            "semesters": rows,
            "today": date.today(),
            "error": error,
            "seasons": _SEASONS,
        },
        status_code=status_code,
    )


@router.get("/semesters", response_class=HTMLResponse)
async def admin_semesters(request: Request, db: DBSession, current_user: AdminUser) -> HTMLResponse:
    return await _render_semesters(request, db, current_user, error=None)


@router.post("/semesters", response_model=None)
async def admin_create_semester(
    request: Request,
    db: DBSession,
    current_user: AdminUser,
    name: str = Form(...),
    season: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    parsed = _parse_semester_form(name, season, start_date, end_date)
    if isinstance(parsed, str):
        return await _render_semesters(request, db, current_user, error=parsed, status_code=422)
    clean_name, clean_season, start, end = parsed
    if await _semester_overlaps(db, start, end, exclude_id=None):
        return await _render_semesters(request, db, current_user, error="overlap", status_code=422)
    db.add(Semester(name=clean_name, season=clean_season, start_date=start, end_date=end))
    await audit(
        db,
        action="create_semester",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        name=clean_name,
        start_date=str(start),
        end_date=str(end),
    )
    await db.commit()
    return RedirectResponse(url="/admin/semesters", status_code=303)


@router.post("/semesters/{semester_id}", response_model=None)
async def admin_update_semester(
    request: Request,
    semester_id: int,
    db: DBSession,
    current_user: AdminUser,
    name: str = Form(...),
    season: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    semester = await db.get(Semester, semester_id)
    if semester is None:
        raise HTTPException(status_code=404)
    parsed = _parse_semester_form(name, season, start_date, end_date)
    if isinstance(parsed, str):
        return await _render_semesters(request, db, current_user, error=parsed, status_code=422)
    clean_name, clean_season, start, end = parsed
    if await _semester_overlaps(db, start, end, exclude_id=semester_id):
        return await _render_semesters(request, db, current_user, error="overlap", status_code=422)
    semester.name, semester.season = clean_name, clean_season
    semester.start_date, semester.end_date = start, end
    await audit(
        db,
        action="update_semester",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        semester_id=semester_id,
        start_date=str(start),
        end_date=str(end),
    )
    await db.commit()
    return RedirectResponse(url="/admin/semesters", status_code=303)
