"""Teacher routes: connect a Google account, link a Classroom course and coursework."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from submissions_checker.api.authz import require_subject_access
from submissions_checker.api.dependencies import (
    AppSettings,
    CurrentUserData,
    DBSession,
    TeacherUser,
)
from submissions_checker.core.config import Settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import GoogleConnectionStatus
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.services.audit import audit
from submissions_checker.services.google import oauth
from submissions_checker.services.google.client import ClassroomClient, GoogleApiError
from submissions_checker.services.google.crypto import decrypt_token, encrypt_token
from submissions_checker.services.llm_grading.config import is_llm_graded, subject_uses_llm

logger = get_logger(__name__)

router = APIRouter(prefix="/teacher", tags=["teacher-classroom"])

STATE_COOKIE = "g_oauth"
STATE_TTL_SECONDS = 600
COOKIE_PATH = "/teacher/google"


async def classroom_http() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Outbound HTTP client for Google calls; tests override this dependency."""
    async with httpx.AsyncClient(timeout=20.0) as http:
        yield http


ClassroomHttp = Depends(classroom_http)


def _back(subject_id: int, **params: str) -> RedirectResponse:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(
        f"/teacher/subjects/{subject_id}" + (f"?{query}" if query else ""), status_code=303
    )


async def _connection_for(db: DBSession, user_id: int) -> GoogleConnection | None:
    return (
        await db.execute(select(GoogleConnection).where(GoogleConnection.user_id == user_id))
    ).scalar_one_or_none()


def _client(settings: Settings, conn: GoogleConnection, http: httpx.AsyncClient) -> ClassroomClient:
    return ClassroomClient(settings, decrypt_token(settings, conn.refresh_token_enc), http)


async def classroom_card_context(
    db: DBSession,
    subject: Subject,
    user: CurrentUserData,
    settings: Settings,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Data for the Google Classroom card; None when no assignment uses the LLM.

    Google failures never propagate: the card renders with an error line instead.
    """
    assignments = list(
        (
            await db.execute(
                select(SubjectsAssignment)
                .where(SubjectsAssignment.subject_id == subject.id)
                .order_by(SubjectsAssignment.deadline.asc().nullslast(), SubjectsAssignment.id)
            )
        )
        .scalars()
        .all()
    )
    if not subject_uses_llm(a.config for a in assignments):
        return None

    connection = await _connection_for(db, user.user_id) if settings.classroom_enabled else None
    llm_assignments = [a for a in assignments if is_llm_graded(a.config)]
    ctx: dict[str, Any] = {
        "enabled": settings.classroom_enabled,
        "connection": connection,
        "needs_reconnect": connection is not None
        and connection.status != GoogleConnectionStatus.ACTIVE.value,
        "course_id": subject.classroom_course_id,
        "course_name": subject.classroom_course_name,
        "synced_at": subject.classroom_synced_at,
        "sync_error": subject.classroom_sync_error,
        "assignments": [
            {
                "id": a.id,
                "title": a.title,
                "coursework_id": a.classroom_coursework_id,
                "coursework_title": a.classroom_coursework_title,
            }
            for a in llm_assignments
        ],
        "courses": [],
        "coursework_options": [],
        "error": None,
        "unmatched": [],
        "name_matched_count": 0,
    }
    if connection is None or ctx["needs_reconnect"] or http is None:
        return ctx
    try:
        client = _client(settings, connection, http)
        if not subject.classroom_course_id:
            ctx["courses"] = await client.list_courses()
        else:
            ctx["coursework_options"] = await client.list_coursework(subject.classroom_course_id)
    except oauth.GoogleAuthError as exc:
        logger.warning("classroom_card_auth_error", error=str(exc))
        if exc.invalid_grant:
            connection.status = GoogleConnectionStatus.ERROR.value
            connection.last_error = str(exc)[:500]
            ctx["needs_reconnect"] = True
        else:
            ctx["error"] = "google"
    except GoogleApiError as exc:
        logger.warning("classroom_card_api_error", error=str(exc))
        ctx["error"] = "google"
    return ctx


# ── OAuth ────────────────────────────────────────────────────────────────────


@router.get("/google/connect")
async def google_connect(
    subject_id: int, db: DBSession, current_user: TeacherUser, settings: AppSettings
) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)
    if not settings.classroom_enabled:
        return _back(subject_id, classroom_error="not_configured")
    state = secrets.token_urlsafe(24)
    pkce = oauth.new_pkce()
    payload = json.dumps(
        {
            "state": state,
            "verifier": pkce.verifier,
            "subject_id": subject_id,
            "uid": current_user.user_id,
        }
    )
    response = RedirectResponse(oauth.authorization_url(settings, state, pkce), status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        encrypt_token(settings, payload),
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path=COOKIE_PATH,
    )
    return response


def _read_state(request: Request, settings: Settings) -> dict[str, Any] | None:
    raw = request.cookies.get(STATE_COOKIE)
    if not raw:
        return None
    try:
        data = json.loads(decrypt_token(settings, raw, ttl=STATE_TTL_SECONDS))
    except (oauth.GoogleAuthError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _finish(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    http: httpx.AsyncClient = ClassroomHttp,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    data = _read_state(request, settings)
    if data is None or not state or not secrets.compare_digest(str(data.get("state")), state):
        return _finish(RedirectResponse("/teacher?classroom_error=state", status_code=303))
    if data.get("uid") != current_user.user_id:
        return _finish(RedirectResponse("/teacher?classroom_error=state", status_code=303))
    subject_id = int(data["subject_id"])
    await require_subject_access(db, subject_id, current_user)
    if error or not code:
        return _finish(_back(subject_id, classroom_error="denied"))
    try:
        email, refresh_token = await oauth.exchange_code(settings, code, data["verifier"], http)
    except (oauth.GoogleAuthError, httpx.HTTPError) as exc:
        logger.warning("google_code_exchange_failed", error=str(exc))
        return _finish(_back(subject_id, classroom_error="exchange"))

    encrypted = encrypt_token(settings, refresh_token)
    conn = await _connection_for(db, current_user.user_id)
    if conn is None:
        conn = GoogleConnection(
            user_id=current_user.user_id, google_email=email, refresh_token_enc=encrypted
        )
        db.add(conn)
    conn.google_email = email
    conn.refresh_token_enc = encrypted
    conn.status = GoogleConnectionStatus.ACTIVE.value
    conn.last_error = None
    await audit(
        db,
        "google_connected",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="user",
        target_id=current_user.user_id,
        google_email=email,
    )
    return _finish(_back(subject_id, classroom="connected"))


@router.post("/google/disconnect")
async def google_disconnect(
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    subject_id: int = Form(...),
    http: httpx.AsyncClient = ClassroomHttp,
) -> RedirectResponse:
    await require_subject_access(db, subject_id, current_user)
    conn = await _connection_for(db, current_user.user_id)
    if conn is not None:
        try:
            await oauth.revoke(decrypt_token(settings, conn.refresh_token_enc), http)
        except oauth.GoogleAuthError:
            pass
        await db.delete(conn)
        await audit(
            db,
            "google_disconnected",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            target_type="user",
            target_id=current_user.user_id,
        )
    return _back(subject_id, classroom="disconnected")


# ── Linking ──────────────────────────────────────────────────────────────────


async def _linking_client(
    db: DBSession,
    user: CurrentUserData,
    settings: Settings,
    http: httpx.AsyncClient,
) -> tuple[GoogleConnection, ClassroomClient]:
    conn = await _connection_for(db, user.user_id)
    if conn is None or conn.status != GoogleConnectionStatus.ACTIVE.value:
        raise HTTPException(status_code=409, detail="Google account is not connected")
    try:
        return conn, _client(settings, conn, http)
    except oauth.GoogleAuthError as exc:
        raise HTTPException(status_code=409, detail="Google connection is unusable") from exc


@router.post("/subjects/{subject_id}/classroom/course")
async def link_course(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    course_id: str = Form(...),
    http: httpx.AsyncClient = ClassroomHttp,
) -> RedirectResponse:
    subject = await require_subject_access(db, subject_id, current_user)
    conn, client = await _linking_client(db, current_user, settings, http)
    try:
        courses = await client.list_courses()
    except (oauth.GoogleAuthError, GoogleApiError):
        return _back(subject_id, classroom_error="google")
    match = next((c for c in courses if c["id"] == course_id), None)
    if match is None:
        raise HTTPException(status_code=422, detail="Unknown Classroom course")
    subject.classroom_course_id = match["id"]
    subject.classroom_course_name = match["name"][:255]
    subject.classroom_connection_id = conn.id
    subject.classroom_synced_at = None
    subject.classroom_sync_error = None
    await audit(
        db,
        "classroom_course_linked",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="subject",
        target_id=subject_id,
        course_id=match["id"],
        linked_at=datetime.now(UTC).isoformat(),
    )
    return _back(subject_id, classroom="course_linked")


@router.post("/subjects/{subject_id}/classroom/coursework")
async def link_coursework(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    assignment_id: int = Form(...),
    coursework_id: str = Form(""),
    http: httpx.AsyncClient = ClassroomHttp,
) -> RedirectResponse:
    subject = await require_subject_access(db, subject_id, current_user)
    assignment = (
        await db.execute(
            select(SubjectsAssignment).where(
                SubjectsAssignment.id == assignment_id,
                SubjectsAssignment.subject_id == subject_id,
            )
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not is_llm_graded(assignment.config):
        raise HTTPException(status_code=422, detail="Assignment is not graded by the LLM")

    if not coursework_id:
        assignment.classroom_coursework_id = None
        assignment.classroom_coursework_title = None
    else:
        if not subject.classroom_course_id:
            raise HTTPException(status_code=422, detail="Link a Classroom course first")
        _, client = await _linking_client(db, current_user, settings, http)
        try:
            works = await client.list_coursework(subject.classroom_course_id)
        except (oauth.GoogleAuthError, GoogleApiError):
            return _back(subject_id, classroom_error="google")
        match = next((w for w in works if w["id"] == coursework_id), None)
        if match is None:
            raise HTTPException(status_code=422, detail="Unknown Classroom coursework")
        assignment.classroom_coursework_id = match["id"]
        assignment.classroom_coursework_title = match["title"][:255]
    await audit(
        db,
        "classroom_coursework_linked",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="assignment",
        target_id=assignment_id,
        coursework_id=coursework_id or None,
    )
    return _back(subject_id, classroom="coursework_linked")
