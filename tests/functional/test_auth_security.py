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
ADMIN_ANALYTICS = "/teacher/analytics"  # AdminUser only (cross-teacher aggregation)
TEACHER_ANALYTICS_STUDENT = "/teacher/analytics/students/1"  # TeacherUser
ANY_AUTHENTICATED = "/notifications"  # CurrentUser

PROTECTED_ENDPOINTS = [
    TEACHER_ONLY,
    STUDENT_ONLY,
    ADMIN_ONLY,
    ADMIN_ANALYTICS,
    ANY_AUTHENTICATED,
]


# ── Open endpoints ───────────────────────────────────────────────────────────


async def test_health_is_open(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


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


async def test_token_signed_with_wrong_secret_is_rejected(
    client: AsyncClient, teacher
) -> None:
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


async def test_student_cannot_access_admin_analytics(student_client: AsyncClient) -> None:
    assert (await student_client.get(ADMIN_ANALYTICS)).status_code == 403


async def test_teacher_cannot_access_student_area(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get(STUDENT_ONLY)).status_code == 403


async def test_teacher_cannot_access_admin_area(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get(ADMIN_ONLY)).status_code == 403


async def test_teacher_cannot_access_admin_analytics(teacher_client: AsyncClient) -> None:
    assert (await teacher_client.get(ADMIN_ANALYTICS)).status_code == 403


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


async def test_admin_can_access_admin_analytics(admin_client: AsyncClient) -> None:
    resp = await admin_client.get(ADMIN_ANALYTICS)
    assert resp.status_code == 200


async def test_any_authenticated_user_sees_notifications(
    student_client: AsyncClient,
) -> None:
    resp = await student_client.get(ANY_AUTHENTICATED)
    assert resp.status_code == 200
