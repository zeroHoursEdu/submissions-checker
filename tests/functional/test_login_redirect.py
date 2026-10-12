"""A browser that opens a protected page without a session lands on the login page."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from submissions_checker.core.security import COOKIE_NAME
from submissions_checker.db.models.enums import UserRole

pytestmark = pytest.mark.asyncio

PASSWORD = "Sup3rSecret!"
BROWSER = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8", "sec-fetch-mode": "navigate"}


async def test_page_navigation_without_session_redirects_to_login(client: AsyncClient) -> None:
    resp = await client.get("/teacher/subjects/1?tab=ops", headers=BROWSER, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login?next=%2Fteacher%2Fsubjects%2F1%3Ftab%3Dops"


async def test_expired_cookie_is_dropped_on_the_way_to_login(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "garbage")
    resp = await client.get("/portal", headers=BROWSER, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/auth/login")
    # Otherwise a token that decodes but belongs to an inactive user bounces forever.
    assert COOKIE_NAME in resp.headers.get("set-cookie", "")


async def test_api_and_fetch_requests_keep_the_plain_401(client: AsyncClient) -> None:
    assert (await client.get("/teacher", follow_redirects=False)).status_code == 401
    fetch = {"accept": "text/html", "sec-fetch-mode": "cors"}
    assert (await client.get("/teacher", headers=fetch, follow_redirects=False)).status_code == 401
    assert (
        await client.post("/teacher/subjects/1/classroom/sync", headers=BROWSER)
    ).status_code == 401


async def test_login_returns_to_the_requested_page(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.TEACHER, username="t1", password=PASSWORD)
    page = await client.get("/auth/login?next=%2Fteacher%2Fsubjects%2F1")
    assert 'name="next" value="/teacher/subjects/1"' in page.text
    resp = await client.post(
        "/auth/login",
        data={"username": "t1", "password": PASSWORD, "next": "/teacher/subjects/1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/subjects/1"


@pytest.mark.parametrize(
    "target", ["https://evil.example/", "//evil.example/", "/\\evil.example", "teacher", ""]
)
async def test_login_ignores_offsite_next(client: AsyncClient, make_user, target: str) -> None:
    await make_user(role=UserRole.TEACHER, username="t2", password=PASSWORD)
    resp = await client.post(
        "/auth/login",
        data={"username": "t2", "password": PASSWORD, "next": target},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/teacher"
