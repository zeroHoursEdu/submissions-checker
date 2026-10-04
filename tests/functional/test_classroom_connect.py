"""Google connect flow, course/coursework linking and the Classroom card."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes.teacher_classroom import classroom_http
from submissions_checker.core.config import Settings, get_settings
from submissions_checker.db.models import Subject, SubjectsAssignment
from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.main import app
from submissions_checker.services.google.crypto import decrypt_token, encrypt_token

pytestmark = pytest.mark.asyncio

KEY = Fernet.generate_key().decode()
LLM_CFG = {"review_mode": "quiz_and_teacher_scores", "llm_grading": {"enabled": True}}


def _settings(*, configured: bool = True) -> Settings:
    kwargs: dict = {"secret_key": "test-secret-key-minimum-32-chars-long"}
    if configured:
        kwargs |= {
            "google_client_id": "cid",
            "google_client_secret": "csecret",
            "google_token_encryption_key": KEY,
        }
    return Settings(**kwargs)


def _google(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/token"):
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
    if "userinfo" in url:
        return httpx.Response(200, json={"email": "t@edu.kpi.ua"})
    if url.endswith("/revoke"):
        return httpx.Response(200)
    if url.endswith("/courses") or "/courses?" in url:
        return httpx.Response(200, json={"courses": [{"id": "c1", "name": "Course 1"}]})
    if "/courseWork" in url:
        return httpx.Response(200, json={"courseWork": [{"id": "w1", "title": "Work 1"}]})
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def google_env():
    app.dependency_overrides[get_settings] = lambda: _settings()

    async def _http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_google)) as c:
            yield c

    app.dependency_overrides[classroom_http] = _http
    yield
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(classroom_http, None)


async def _subject(db, teacher, *, llm: bool = True):
    subject = Subject(name="S", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    asg = SubjectsAssignment(
        subject_id=subject.id, title="Lab", code="lab", config=LLM_CFG if llm else {}
    )
    db.add(asg)
    await db.commit()
    return subject, asg


async def _connect(db, teacher):
    db.add(
        GoogleConnection(
            user_id=teacher.id,
            google_email="t@edu.kpi.ua",
            refresh_token_enc=encrypt_token(_settings(), "rt"),
        )
    )
    await db.commit()


async def test_card_hidden_without_llm_assignment(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher, llm=False)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    assert "classroom-card" not in resp.text


async def test_card_shows_connect_when_llm_assignment(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert f"/teacher/google/connect?subject_id={subject.id}" in resp.text


async def test_card_shows_admin_hint_when_google_not_configured(
    teacher_client: AsyncClient, db, teacher
):
    app.dependency_overrides[get_settings] = lambda: _settings(configured=False)
    subject, _ = await _subject(db, teacher)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "GOOGLE_CLIENT_ID" in resp.text
    assert "/teacher/google/connect" not in resp.text


async def test_card_lists_courses_then_coursework(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    await _connect(db, teacher)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert 'value="c1"' in resp.text
    await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/course", data={"course_id": "c1"}
    )
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert 'value="w1"' in resp.text


async def test_card_survives_google_failure(teacher_client: AsyncClient, db, teacher):
    async def _boom():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as c:
            yield c

    app.dependency_overrides[classroom_http] = _boom
    subject, _ = await _subject(db, teacher)
    await _connect(db, teacher)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    assert "classroom-card" in resp.text
    assert "Не вдалося отримати дані з Google" in resp.text


async def test_connect_redirects_to_google_with_state_cookie(
    teacher_client: AsyncClient, db, teacher
):
    subject, _ = await _subject(db, teacher)
    resp = await teacher_client.get(f"/teacher/google/connect?subject_id={subject.id}")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("https://accounts.google.com/")
    cookie = resp.headers["set-cookie"]
    assert "g_oauth=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/teacher/google" in cookie
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["state"][0] not in cookie
    payload = json.loads(
        decrypt_token(_settings(), cookie.split("g_oauth=")[1].split(";")[0].strip('"'))
    )
    assert payload["verifier"] not in cookie


async def _start(teacher_client, subject) -> tuple[str, str]:
    resp = await teacher_client.get(f"/teacher/google/connect?subject_id={subject.id}")
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    return state, resp.headers["set-cookie"]


async def test_callback_rejects_state_mismatch(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    await _start(teacher_client, subject)
    resp = await teacher_client.get("/teacher/google/complete?code=x&state=wrong")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{subject.id}?classroom_error=state"
    assert (await db.execute(select(GoogleConnection))).scalars().all() == []


async def test_complete_without_cookie_goes_to_dashboard(teacher_client: AsyncClient):
    resp = await teacher_client.get("/teacher/google/complete?code=x&state=y")
    assert resp.headers["location"] == "/teacher?classroom_error=state"
    page = await teacher_client.get("/teacher?classroom_error=state")
    assert "Не вдалося підтвердити запит" in page.text


async def test_expired_state_cookie_is_rejected(teacher_client: AsyncClient, db, teacher):
    import time

    subject, _ = await _subject(db, teacher)
    payload = json.dumps(
        {"state": "s", "verifier": "v", "subject_id": subject.id, "uid": teacher.id}
    )
    old = Fernet(KEY.encode()).encrypt_at_time(payload.encode(), int(time.time()) - 3600)
    teacher_client.cookies.set("g_oauth", old.decode())
    resp = await teacher_client.get("/teacher/google/complete?code=x&state=s")
    assert "classroom_error=state" in resp.headers["location"]
    assert (await db.execute(select(GoogleConnection))).scalars().all() == []


async def test_callback_is_unauthenticated_bounce(client: AsyncClient, db):
    resp = await client.get('/teacher/google/callback?code=a%26b&state="><script>x</script>')
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "<script>" not in resp.text
    assert "url=/teacher/google/complete?code=a%26b&amp;state=" in resp.text
    assert "%3Cscript%3E" in resp.text
    assert (await db.execute(select(GoogleConnection))).scalars().all() == []


async def test_callback_forwards_google_error(client: AsyncClient):
    resp = await client.get("/teacher/google/callback?error=access_denied&state=s")
    assert "complete?error=access_denied" in resp.text


async def test_callback_stores_encrypted_token(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    state, _ = await _start(teacher_client, subject)
    resp = await teacher_client.get(f"/teacher/google/complete?code=abc&state={state}")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/teacher/subjects/{subject.id}")
    conn = (await db.execute(select(GoogleConnection))).scalar_one()
    assert conn.refresh_token_enc != "rt"
    assert decrypt_token(_settings(), conn.refresh_token_enc) == "rt"
    assert conn.google_email == "t@edu.kpi.ua"
    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "google_connected" in actions


async def test_callback_access_denied_redirects_back(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    state, _ = await _start(teacher_client, subject)
    resp = await teacher_client.get(f"/teacher/google/complete?error=access_denied&state={state}")
    assert resp.status_code == 303
    assert "classroom_error=denied" in resp.headers["location"]


async def test_callback_cookie_of_other_user_is_rejected(
    teacher_client: AsyncClient, db, teacher, make_user
):
    from tests.functional.conftest import authenticate

    subject, _ = await _subject(db, teacher)
    state, _ = await _start(teacher_client, subject)
    other = await make_user(role=UserRole.TEACHER, username="other")
    authenticate(teacher_client, other)
    resp = await teacher_client.get(f"/teacher/google/complete?code=abc&state={state}")
    assert "classroom_error=state" in resp.headers["location"]
    assert (await db.execute(select(GoogleConnection))).scalars().all() == []


async def test_disconnect_deletes_row(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    await _connect(db, teacher)
    resp = await teacher_client.post(
        "/teacher/google/disconnect", data={"subject_id": str(subject.id)}
    )
    assert resp.status_code == 303
    assert (await db.execute(select(GoogleConnection))).scalars().all() == []


async def test_link_course_and_coursework(teacher_client: AsyncClient, db, teacher):
    subject, asg = await _subject(db, teacher)
    await _connect(db, teacher)
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/course", data={"course_id": "c1"}
    )
    assert r.status_code == 303
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/coursework",
        data={"assignment_id": str(asg.id), "coursework_id": "w1"},
    )
    assert r.status_code == 303
    await db.refresh(subject)
    await db.refresh(asg)
    assert subject.classroom_course_id == "c1"
    assert subject.classroom_course_name == "Course 1"
    assert asg.classroom_coursework_id == "w1"
    assert asg.classroom_coursework_title == "Work 1"
    # empty id unlinks
    await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/coursework",
        data={"assignment_id": str(asg.id), "coursework_id": ""},
    )
    await db.refresh(asg)
    assert asg.classroom_coursework_id is None


async def test_link_rejects_foreign_course_id(teacher_client: AsyncClient, db, teacher):
    subject, _ = await _subject(db, teacher)
    await _connect(db, teacher)
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/course", data={"course_id": "nope"}
    )
    assert r.status_code == 422


async def test_link_rejects_non_llm_assignment(teacher_client: AsyncClient, db, teacher):
    subject, asg = await _subject(db, teacher, llm=False)
    await _connect(db, teacher)
    await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/course", data={"course_id": "c1"}
    )
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/coursework",
        data={"assignment_id": str(asg.id), "coursework_id": "w1"},
    )
    assert r.status_code == 422


async def test_other_teacher_cannot_link(teacher_client: AsyncClient, db, make_user):
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    subject, _ = await _subject(db, owner)
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/course", data={"course_id": "c1"}
    )
    assert r.status_code in (403, 404)
