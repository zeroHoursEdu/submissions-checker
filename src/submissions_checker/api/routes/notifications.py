"""In-app notification routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from submissions_checker.api.dependencies import CurrentUser, DBSession
from submissions_checker.db.models.notification import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def notifications_list(
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
) -> HTMLResponse:
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.user_id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    notifications = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={"current_user": current_user, "notifications": notifications},
    )


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    db: DBSession,
    current_user: CurrentUser,
) -> RedirectResponse:
    notification = await db.get(Notification, notification_id)
    if notification is None or notification.user_id != current_user.user_id:
        raise HTTPException(status_code=404)
    notification.is_read = True
    notification.read_at = datetime.now(UTC)
    await db.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.post("/read-all")
async def mark_all_read(
    db: DBSession,
    current_user: CurrentUser,
) -> RedirectResponse:
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == current_user.user_id,
            Notification.is_read.is_(False),
        )
    )
    now = datetime.now(UTC)
    for n in result.scalars().all():
        n.is_read = True
        n.read_at = now
    await db.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.get("/unread-count")
async def unread_count(
    db: DBSession,
    current_user: CurrentUser,
) -> JSONResponse:
    from sqlalchemy import func
    result = await db.execute(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user.user_id,
            Notification.is_read.is_(False),
        )
    )
    count = result.scalar_one() or 0
    return JSONResponse({"count": count})
