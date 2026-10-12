"""Teacher routes: connect a Google account, link a Classroom course and coursework."""

from __future__ import annotations

import html
import json
import secrets
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from submissions_checker.api.authz import require_subject_access
from submissions_checker.api.dependencies import (
    AppSettings,
    CurrentUserData,
    DBSession,
    TeacherUser,
)
from submissions_checker.core.config import Settings
from submissions_checker.core.logging import get_logger
from submissions_checker.core.templates import render
from submissions_checker.db.models.classroom import ClassroomStudentLink, ClassroomWork, LLMGrading
from submissions_checker.db.models.enums import (
    ClassroomLinkMethod,
    GoogleConnectionStatus,
    LLMGradingStatus,
    UserRole,
)
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject, SubjectsStudents
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.user import User
from submissions_checker.services import teacher_scores
from submissions_checker.services.audit import audit
from submissions_checker.services.google import oauth, sync_runs
from submissions_checker.services.google.client import ClassroomClient, GoogleApiError
from submissions_checker.services.google.coursework_match import suggest
from submissions_checker.services.google.crypto import decrypt_token, encrypt_token
from submissions_checker.services.google.ingest import (
    release_waiting,
)
from submissions_checker.services.google.matching import BULK_CONFIRM
from submissions_checker.services.llm_grading.config import (
    is_llm_graded,
    llm_criteria,
    subject_uses_llm,
)
from submissions_checker.services.storage import get_storage

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


async def _bulk_confirmable(db: DBSession, subject_id: int) -> list[ClassroomStudentLink]:
    """Unconfirmed name matches that are exact enough to confirm in one click."""
    return list(
        (
            await db.execute(
                select(ClassroomStudentLink)
                .where(
                    ClassroomStudentLink.subject_id == subject_id,
                    ClassroomStudentLink.method == ClassroomLinkMethod.NAME.value,
                    ClassroomStudentLink.confirmed.is_(False),
                    ClassroomStudentLink.score >= BULK_CONFIRM,
                )
                .order_by(ClassroomStudentLink.id)
            )
        )
        .scalars()
        .all()
    )


async def _fill_unmatched(db: DBSession, subject_id: int, ctx: dict[str, Any]) -> None:
    """Unmatched roster entries (newest first) and the count of bulk-confirmable name matches."""
    unmatched = list(
        (
            await db.execute(
                select(ClassroomStudentLink)
                .where(
                    ClassroomStudentLink.subject_id == subject_id,
                    ClassroomStudentLink.method == ClassroomLinkMethod.NONE.value,
                )
                .order_by(ClassroomStudentLink.id.desc())
            )
        )
        .scalars()
        .all()
    )
    ctx["unmatched"] = [
        {
            "id": link.id,
            "name": link.classroom_name,
            "email": link.classroom_email,
            "candidates": [
                cand
                for cand in (link.candidates if isinstance(link.candidates, list) else [])
                if isinstance(cand, dict) and "student_id" in cand
            ][:3],
        }
        for link in unmatched
    ]
    ctx["name_matched_count"] = len(await _bulk_confirmable(db, subject_id))
    ctx["enrolled_students"] = []
    if unmatched:
        rows = await db.execute(
            select(Student.id, Student.full_name)
            .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
            .where(SubjectsStudents.subject_id == subject_id)
            .order_by(Student.full_name)
        )
        ctx["enrolled_students"] = [{"id": i, "name": n} for i, n in rows.all()]


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
    await _fill_unmatched(db, subject.id, ctx)
    if connection is None or ctx["needs_reconnect"] or http is None:
        return ctx
    try:
        client = _client(settings, connection, http)
        if not subject.classroom_course_id:
            ctx["courses"] = await client.list_courses()
        else:
            ctx["coursework_options"] = await client.list_coursework(subject.classroom_course_id)
            _suggest_coursework(ctx, assignments)
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


def _suggest_coursework(ctx: dict[str, Any], assignments: list[SubjectsAssignment]) -> None:
    """Pre-select a guessed coursework for every unlinked row of the batch form."""
    rows = ctx["assignments"]
    taken = {a.classroom_coursework_id for a in assignments if a.classroom_coursework_id}
    guesses = suggest(
        [(r["id"], r["title"]) for r in rows if not r["coursework_id"]],
        [(w["id"], w["title"]) for w in ctx["coursework_options"]],
        taken=taken,
    )
    for r in rows:
        r["suggested"] = guesses.get(r["id"])


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


@router.get("/google/callback", response_class=HTMLResponse)
async def google_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    """Landing page for Google's cross-site redirect.

    The session cookie is SameSite=Strict, so it is not sent on this navigation. This
    route is therefore unauthenticated and only bounces, same-origin, to `/complete`.
    """
    # The state travels with an error too: /complete must verify it to know the subject
    # and report "denied" (not a bare state failure).
    params = (
        {"error": error, "state": state or ""}
        if error
        else {"code": code or "", "state": state or ""}
    )
    target = html.escape("/teacher/google/complete?" + urlencode(params), quote=True)
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={target}">'
        f'<title>Google</title></head><body><a href="{target}">Continue</a></body></html>'
    )
    return HTMLResponse(
        page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )


def _state_failure(data: dict[str, Any] | None) -> RedirectResponse:
    subject_id = data.get("subject_id") if data else None
    if isinstance(subject_id, int):
        return _finish(_back(subject_id, classroom_error="state"))
    return _finish(RedirectResponse("/teacher?classroom_error=state", status_code=303))


@router.get("/google/complete")
async def google_complete(
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
    if (
        data is None
        or not state
        or not secrets.compare_digest(str(data.get("state")), state)
        or data.get("uid") != current_user.user_id
    ):
        return _state_failure(data)
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
        try:
            async with db.begin_nested():
                db.add(conn)
        except IntegrityError:
            conn = await _connection_for(db, current_user.user_id)
    if conn is not None:
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


@router.post("/subjects/{subject_id}/classroom/coursework/batch")
async def link_coursework_batch(
    request: Request,
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
    http: httpx.AsyncClient = ClassroomHttp,
) -> RedirectResponse:
    """Link every LLM-graded assignment at once from ``coursework_<assignment id>`` fields.

    All or nothing: a coursework picked twice (or already feeding an assignment left out of
    the form) or one unknown to Classroom saves no row. Absent fields leave a row as it is;
    an empty value unlinks it.
    """
    subject = await require_subject_access(db, subject_id, current_user)
    form = await request.form()
    assignments = list(
        (
            await db.execute(
                select(SubjectsAssignment).where(SubjectsAssignment.subject_id == subject_id)
            )
        )
        .scalars()
        .all()
    )
    wanted: dict[int, str] = {}
    for a in assignments:
        value = form.get(f"coursework_{a.id}")
        if isinstance(value, str) and is_llm_graded(a.config):
            wanted[a.id] = value.strip()
    final = {a.id: wanted.get(a.id, a.classroom_coursework_id or "") for a in assignments}
    picked = [cw for cw in final.values() if cw]
    if len(picked) != len(set(picked)):
        # One coursework feeding two assignments would ingest (and grade) every
        # submission twice, against two different task descriptions.
        return _back(subject_id, classroom_error="coursework_taken")

    titles: dict[str, str] = {}
    if any(wanted.values()):
        if not subject.classroom_course_id:
            raise HTTPException(status_code=422, detail="Link a Classroom course first")
        _, client = await _linking_client(db, current_user, settings, http)
        try:
            titles = {
                w["id"]: w["title"]
                for w in await client.list_coursework(subject.classroom_course_id)
            }
        except (oauth.GoogleAuthError, GoogleApiError):
            return _back(subject_id, classroom_error="google")
        if any(cw and cw not in titles for cw in wanted.values()):
            raise HTTPException(status_code=422, detail="Unknown Classroom coursework")

    for a in assignments:
        if a.id not in wanted or wanted[a.id] == (a.classroom_coursework_id or ""):
            continue
        cw = wanted[a.id]
        a.classroom_coursework_id = cw or None
        a.classroom_coursework_title = titles[cw][:255] if cw else None
        await audit(
            db,
            "classroom_coursework_linked",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            target_type="assignment",
            target_id=a.id,
            coursework_id=cw or None,
        )
    return _back(subject_id, classroom="coursework_linked")


# ── Sync ─────────────────────────────────────────────────────────────────────


@router.post("/subjects/{subject_id}/classroom/sync")
async def sync_now(
    subject_id: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
) -> RedirectResponse:
    """Start the nightly ingest for this subject now, in the background.

    Returns at once; the progress panel polls ``/teacher/classroom/sync/active``.
    """
    subject = await require_subject_access(db, subject_id, current_user)
    if not settings.classroom_enabled:
        return _back(subject_id, classroom_error="not_configured")
    conn = (
        await db.get(GoogleConnection, subject.classroom_connection_id)
        if subject.classroom_connection_id
        else None
    )
    if (
        not subject.classroom_course_id
        or conn is None
        or conn.status != GoogleConnectionStatus.ACTIVE.value
    ):
        return _back(subject_id, classroom_error="not_linked")
    storage = get_storage(settings)
    if storage is None:
        return _back(subject_id, classroom_error="storage")
    engine = db.bind
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("session is not bound to an engine")
    # The background task outlives this session: detach the loaded connection so the
    # commit below does not expire the token it needs.
    db.expunge(conn)
    started = await sync_runs.start(db, subject_id, current_user.user_id)
    if started is None:
        return _back(subject_id, classroom_error="busy")
    sync_runs.spawn(
        sync_runs.run(
            engine,
            started.id,
            subject_id,
            lambda http: _client(settings, conn, http),
            storage,
            sync_runs.Actor(current_user.user_id, current_user.username),
        )
    )
    return _back(subject_id, classroom="sync_started")


@router.get("/classroom/sync/active")
async def sync_active(db: DBSession, current_user: TeacherUser) -> JSONResponse:
    """Manual syncs the progress panel shows: running, or just finished, in own subjects."""
    query = select(Subject.id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Subject.owner_id == current_user.user_id)
    subject_ids = list((await db.execute(query)).scalars())
    return JSONResponse(
        {"runs": await sync_runs.visible_runs(db, subject_ids)},
        headers={"Cache-Control": "no-store"},
    )


# ── Student matching ─────────────────────────────────────────────────────────


async def _student_taken(
    db: DBSession, subject_id: int, student_id: int, except_link_id: int
) -> bool:
    return (
        await db.execute(
            select(ClassroomStudentLink.id).where(
                ClassroomStudentLink.subject_id == subject_id,
                ClassroomStudentLink.student_id == student_id,
                ClassroomStudentLink.confirmed.is_(True),
                ClassroomStudentLink.id != except_link_id,
            )
        )
    ).first() is not None


@router.post("/subjects/{subject_id}/classroom/links/confirm-all")
async def confirm_all_links(
    subject_id: int, db: DBSession, current_user: TeacherUser
) -> RedirectResponse:
    """Confirm every name match at or above the bulk threshold."""
    await require_subject_access(db, subject_id, current_user)
    taken = set(
        (
            await db.execute(
                select(ClassroomStudentLink.student_id).where(
                    ClassroomStudentLink.subject_id == subject_id,
                    ClassroomStudentLink.confirmed.is_(True),
                    ClassroomStudentLink.student_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    confirmed = 0
    for link in await _bulk_confirmable(db, subject_id):
        if link.student_id in taken:
            continue
        link.method = ClassroomLinkMethod.MANUAL.value
        link.confirmed = True
        taken.add(link.student_id)
        confirmed += 1
    await audit(
        db,
        "classroom_link_confirm_all",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="subject",
        target_id=subject_id,
        count=confirmed,
    )
    return _back(subject_id, classroom="links_confirmed", synced=str(confirmed))


@router.post("/subjects/{subject_id}/classroom/links/{link_id}")
async def resolve_link(
    subject_id: int,
    link_id: int,
    db: DBSession,
    current_user: TeacherUser,
    action: str = Form(...),
    student_id: int | None = Form(None),
    next_url: str | None = Form(None, alias="next"),
) -> RedirectResponse:
    """Resolve one roster entry: link it to a student, ignore it, or confirm a name match.

    ``next`` (the assignment board) is honoured only inside this subject's pages.
    """
    await require_subject_access(db, subject_id, current_user)
    link = (
        await db.execute(
            select(ClassroomStudentLink)
            .where(
                ClassroomStudentLink.id == link_id,
                ClassroomStudentLink.subject_id == subject_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")

    if action == "link":
        if student_id is None:
            return _back(subject_id, classroom_error="pick_student")
        enrolled = (
            await db.execute(
                select(SubjectsStudents.student_id).where(
                    SubjectsStudents.subject_id == subject_id,
                    SubjectsStudents.student_id == student_id,
                )
            )
        ).first()
        if enrolled is None:
            raise HTTPException(status_code=422, detail="Student is not enrolled in this subject")
        if await _student_taken(db, subject_id, student_id, link.id):
            raise HTTPException(status_code=409, detail="Student is already linked")
        link.method = ClassroomLinkMethod.MANUAL.value
        link.confirmed = True
        link.student_id = student_id
        await release_waiting(db, link)
    elif action == "ignore":
        link.method = ClassroomLinkMethod.IGNORED.value
        link.confirmed = True
        link.student_id = None
    elif action == "confirm":
        if link.method != ClassroomLinkMethod.NAME.value or link.student_id is None:
            return _back(subject_id, classroom_error="not_name_match")
        if await _student_taken(db, subject_id, link.student_id, link.id):
            raise HTTPException(status_code=409, detail="Student is already linked")
        link.method = ClassroomLinkMethod.MANUAL.value
        link.confirmed = True
        await release_waiting(db, link)
    else:
        return _back(subject_id, classroom_error="bad_action")

    await audit(
        db,
        f"classroom_link_{action}",
        actor_id=current_user.user_id,
        actor_username=current_user.username,
        target_type="classroom_student_link",
        target_id=link_id,
        student_id=link.student_id,
    )
    if next_url and next_url.startswith(f"/teacher/subjects/{subject_id}/"):
        return RedirectResponse(next_url, status_code=303)
    return _back(subject_id, classroom="link_" + action)


# ── Board actions: retry a failed grading, download a fetched file ──────────


@router.post("/subjects/{subject_id}/classroom/gradings/{grading_id}/retry")
async def retry_grading(
    subject_id: int, grading_id: int, db: DBSession, current_user: TeacherUser
) -> RedirectResponse:
    """Queue a FAILED grading again; the nightly job grades it next night."""
    await require_subject_access(db, subject_id, current_user)
    row = (
        await db.execute(
            select(LLMGrading, SubjectsAssignment.id)
            .join(ClassroomWork, ClassroomWork.id == LLMGrading.classroom_work_id)
            .join(SubjectsAssignment, SubjectsAssignment.id == ClassroomWork.subjects_assignment_id)
            .where(LLMGrading.id == grading_id, SubjectsAssignment.subject_id == subject_id)
            .with_for_update(of=LLMGrading)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Grading not found")
    grading, sa_id = row
    if grading.status == LLMGradingStatus.FAILED.value:
        grading.status = LLMGradingStatus.PENDING.value
        grading.attempts = 0
        grading.error = None
        await audit(
            db,
            "llm_grading_retry",
            actor_id=current_user.user_id,
            actor_username=current_user.username,
            target_type="llm_grading",
            target_id=grading_id,
            subject_id=subject_id,
        )
    return RedirectResponse(f"/teacher/subjects/{subject_id}/assignments/{sa_id}", status_code=303)


@router.get(
    "/subjects/{subject_id}/classroom/gradings/{grading_id}/report", response_class=HTMLResponse
)
async def grading_report(
    request: Request,
    subject_id: int,
    grading_id: int,
    db: DBSession,
    current_user: TeacherUser,
) -> HTMLResponse:
    """Teacher-only: why the overnight grader gave this work its points, per criterion."""
    await require_subject_access(db, subject_id, current_user)
    row = (
        await db.execute(
            select(LLMGrading, ClassroomWork, ClassroomStudentLink, SubjectsAssignment)
            .join(ClassroomWork, ClassroomWork.id == LLMGrading.classroom_work_id)
            .join(ClassroomStudentLink, ClassroomStudentLink.id == ClassroomWork.link_id)
            .join(SubjectsAssignment, SubjectsAssignment.id == ClassroomWork.subjects_assignment_id)
            .where(LLMGrading.id == grading_id, SubjectsAssignment.subject_id == subject_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Grading not found")
    grading, work, link, assignment = row
    student = await db.get(Student, link.student_id) if link.student_id else None
    saved: dict[str, Any] | None = None
    if student is not None:
        saved = await db.scalar(
            select(StudentAssignment.teacher_scores).where(
                StudentAssignment.student_id == student.id,
                StudentAssignment.subjects_assignment_id == assignment.id,
            )
        )
    grading_cfg = (assignment.config or {}).get("grading")
    llm_keys = {c.key for c in llm_criteria(grading_cfg)}
    draft = (grading.draft or {}).get("criteria") or {}
    rows = []
    for c in teacher_scores.criteria(grading_cfg):
        verdict = draft.get(c.key) or {}
        saved_points = (saved or {}).get(c.key) if grading.approved_at else None
        rows.append(
            {
                "title": c.title,
                "max": c.max,
                "by_llm": c.key in llm_keys,
                "points": verdict.get("points"),
                "justification": verdict.get("justification"),
                "evidence": verdict.get("evidence"),
                "saved": saved_points,
                "changed": saved_points is not None
                and verdict.get("points") is not None
                and saved_points != verdict.get("points"),
            }
        )
    approver = await db.get(User, grading.approved_by) if grading.approved_by else None
    return render(
        request,
        "teacher_llm_report.html",
        {
            "current_user": current_user,
            "subject_id": subject_id,
            "assignment": assignment,
            "grading": grading,
            "work": work,
            "link": link,
            "student": student,
            "criteria": rows,
            "comment": (grading.draft or {}).get("comment"),
            "approver": approver,
        },
    )


@router.get("/subjects/{subject_id}/classroom/works/{work_id}/files/{idx}")
async def work_file(
    subject_id: int,
    work_id: int,
    idx: int,
    db: DBSession,
    current_user: TeacherUser,
    settings: AppSettings,
) -> Response:
    """Serve one fetched Classroom file from object storage (never a direct storage URL)."""
    await require_subject_access(db, subject_id, current_user)
    work = (
        await db.execute(
            select(ClassroomWork)
            .join(SubjectsAssignment, SubjectsAssignment.id == ClassroomWork.subjects_assignment_id)
            .where(ClassroomWork.id == work_id, SubjectsAssignment.subject_id == subject_id)
        )
    ).scalar_one_or_none()
    manifest = work.manifest if work is not None else []
    entry = manifest[idx] if 0 <= idx < len(manifest) else None
    if entry is None or entry.get("skipped") or not entry.get("storage_key"):
        raise HTTPException(status_code=404, detail="File not found")
    storage = get_storage(settings)
    if storage is None:
        raise HTTPException(status_code=404, detail="File storage is not configured")
    try:
        data = await storage.download_bytes(entry["storage_key"])
    except Exception as exc:  # object missing or storage unreachable
        logger.warning("classroom_file_unreadable", work_id=work_id, idx=idx, error=str(exc))
        raise HTTPException(status_code=404, detail="File is no longer available") from exc
    name = str(entry.get("name") or f"file-{idx}")
    return Response(
        content=data,
        media_type=str(entry.get("mime") or "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"},
    )
