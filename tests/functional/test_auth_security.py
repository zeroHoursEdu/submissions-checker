"""Authentication & role-based authorization matrix.

This is the security backbone: it proves the cookie/JWT auth stack and the
``_require_teacher`` / ``_require_student`` / ``_require_admin`` guards behave
correctly for anonymous, invalid, tampered, deleted, inactive, and wrong-role
callers across a representative endpoint from each protected area.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from jose import jwt

from submissions_checker.core.security import COOKIE_NAME, JWT_ALGORITHM
from submissions_checker.db.models.enums import UserRole
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio

# (path, roles that are allowed). Every other authenticated role must get 403.
TEACHER_ONLY = "/teacher"  # TeacherUser → TEACHER or ADMIN
STUDENT_ONLY = "/portal"  # StudentUser → STUDENT only
ADMIN_ONLY = "/admin"  # AdminUser → ADMIN only
ANY_AUTHENTICATED = "/notifications"  # CurrentUser

PROTECTED_ENDPOINTS = [
    TEACHER_ONLY,
    STUDENT_ONLY,
    ADMIN_ONLY,
    ANY_AUTHENTICATED,
]


# ── Open endpoints ───────────────────────────────────────────────────────────


# ── Sign-in page discloses nothing in production ─────────────────────────────


async def test_login_page_hides_demo_credentials_in_production(
    client: AsyncClient, monkeypatch
) -> None:
    """A production sign-in page must not name an account or a password.

    The hint is a developer convenience. It shipped to a public deployment and
    advertised `teacher / teacher123` to every visitor, which is a username list
    handed to anyone who loads the page.
    """
    from submissions_checker.core import templates as templates_module

    monkeypatch.setattr(templates_module._settings, "environment", "production")

    resp = await client.get("/auth/login")

    assert resp.status_code == 200
    body = resp.text
    for secret in ("teacher123", "student123", "Демо-акаунти"):
        assert secret not in body, f"production login page leaked {secret!r}"


async def test_login_page_shows_demo_credentials_in_development(
    client: AsyncClient, monkeypatch
) -> None:
    """Locally the hint stays: the accounts it describes do exist there."""
    from submissions_checker.core import templates as templates_module

    monkeypatch.setattr(templates_module._settings, "environment", "development")

    resp = await client.get("/auth/login")

    assert resp.status_code == 200
    assert "teacher123" in resp.text


async def test_every_page_carries_security_headers(client: AsyncClient) -> None:
    """The headers are set by the app, so a deployment without the Caddy layer is not
    silently bare."""
    resp = await client.get("/auth/login")
    assert resp.status_code == 200
    assert "frame-ancestors 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "camera=(self)" in resp.headers["permissions-policy"]


async def test_health_is_open(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


async def test_version_is_open(client: AsyncClient) -> None:
    """CI polls /version with no credentials to confirm a build reached the host."""
    resp = await client.get("/version")
    assert resp.status_code == 200
    assert set(resp.json()) == {"revision"}


async def test_version_reports_unknown_when_not_built_in(client: AsyncClient) -> None:
    """A locally built image carries no revision and must say so, not fail."""
    from submissions_checker.api.routes import health

    assert health.APP_REVISION == "unknown"


async def test_readiness_uses_overridden_db(client: AsyncClient) -> None:
    """Confirms the get_db override is wired: readiness hits the test DB and passes."""
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "database": "connected"}


async def test_root_redirects_to_login(client: AsyncClient) -> None:
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/auth/login"


# ── Authentication failures (401) ────────────────────────────────────────────


@pytest.mark.parametrize("path", PROTECTED_ENDPOINTS)
async def test_anonymous_is_rejected(client: AsyncClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status_code == 401


@pytest.mark.parametrize("path", PROTECTED_ENDPOINTS)
async def test_garbage_token_is_rejected(client: AsyncClient, path: str) -> None:
    client.cookies.set(COOKIE_NAME, "not-a-jwt")
    resp = await client.get(path)
    assert resp.status_code == 401


async def test_token_signed_with_wrong_secret_is_rejected(client: AsyncClient, teacher) -> None:
    forged = jwt.encode(
        {
            "sub": str(teacher.id),
            "username": teacher.username,
            "role": teacher.role.value,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "an-entirely-different-secret-key-32chars",
        algorithm=JWT_ALGORITHM,
    )
    client.cookies.set(COOKIE_NAME, forged)
    resp = await client.get(TEACHER_ONLY)
    assert resp.status_code == 401


async def test_expired_token_is_rejected(client: AsyncClient, teacher) -> None:
    from submissions_checker.core.config import get_settings

    expired = jwt.encode(
        {
            "sub": str(teacher.id),
            "username": teacher.username,
            "role": teacher.role.value,
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        get_settings().secret_key,
        algorithm=JWT_ALGORITHM,
    )
    client.cookies.set(COOKIE_NAME, expired)
    resp = await client.get(TEACHER_ONLY)
    assert resp.status_code == 401


async def test_deleted_user_token_is_rejected(client: AsyncClient, db, teacher) -> None:
    authenticate(client, teacher)
    await db.delete(teacher)
    await db.commit()
    resp = await client.get(TEACHER_ONLY)
    assert resp.status_code == 401


async def test_inactive_user_is_rejected(client: AsyncClient, make_user) -> None:
    user = await make_user(role=UserRole.TEACHER, is_active=False)
    authenticate(client, user)
    resp = await client.get(TEACHER_ONLY)
    assert resp.status_code == 401


# ── Role authorization (403) ─────────────────────────────────────────────────


async def test_student_cannot_access_teacher_area(student_client: AsyncClient) -> None:
    assert (await student_client.get(TEACHER_ONLY)).status_code == 403


async def test_student_cannot_access_admin_area(student_client: AsyncClient) -> None:
    assert (await student_client.get(ADMIN_ONLY)).status_code == 403


async def test_teacher_cannot_access_student_area(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get(STUDENT_ONLY)).status_code == 403


async def test_teacher_cannot_access_admin_area(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get(ADMIN_ONLY)).status_code == 403


# ── Positive role access ─────────────────────────────────────────────────────


async def test_teacher_can_access_teacher_dashboard(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get(TEACHER_ONLY)
    assert resp.status_code == 200


async def test_admin_is_accepted_on_teacher_area(admin_client: AsyncClient) -> None:
    """ADMIN satisfies the teacher guard (TEACHER or ADMIN)."""
    resp = await admin_client.get(TEACHER_ONLY)
    assert resp.status_code == 200


async def test_admin_can_access_admin_dashboard(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(ADMIN_ONLY)
    assert resp.status_code == 200


async def test_any_authenticated_user_sees_notifications(
    student_client: AsyncClient,
) -> None:
    resp = await student_client.get(ANY_AUTHENTICATED)
    assert resp.status_code == 200


async def test_analytics_routes_are_gone(admin_client: AsyncClient) -> None:
    """The DB-report analytics pages were replaced by Prometheus metrics (see
    docs/superpowers/specs/2026-09-15-prometheus-grafana-observability-design.md)."""
    for path in ("/teacher/analytics", "/teacher/analytics/fraud", "/teacher/analytics/students/1"):
        assert (await admin_client.get(path)).status_code == 404, path


# ── CSRF: cross-site state-changing requests are refused ──────────────────────


async def test_cross_site_post_is_refused_by_origin(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_cross_site_post_is_refused_by_fetch_metadata(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://test"},
    )
    assert r.status_code == 403


async def test_same_origin_post_passes(client: AsyncClient) -> None:
    r = await client.post(
        "/auth/login",
        data={"username": "x", "password": "y"},
        headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 401  # reached the handler; bad credentials


async def test_get_is_never_origin_checked(client: AsyncClient) -> None:
    r = await client.get("/auth/login", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
