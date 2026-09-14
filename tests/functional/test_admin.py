"""Admin area coverage — ``/admin*`` (ADMIN role only).

Complements ``test_auth_security.py`` (the role matrix on a single representative
endpoint). Here we assert: every admin endpoint is gated (anon→401,
teacher/student→403); teacher creation produces a real TEACHER user with a bcrypt
hash and an audit entry; toggle-active flips ``is_active`` and audits it; the self
and missing-user guards; and that the read-only admin pages render for an admin.
"""

from __future__ import annotations

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.user import User

pytestmark = pytest.mark.asyncio


# (method, path) for every endpoint under the /admin prefix.
ADMIN_GET_ENDPOINTS = [
    "/admin",
    "/admin/users",
    "/admin/teachers/create",
    "/admin/audit",
]
ADMIN_POST_ENDPOINTS = [
    ("/admin/teachers/create", {"username": "x", "password": "longenough1"}),
    ("/admin/users/1/toggle-active", {}),
]


# ── Auth gates ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ADMIN_GET_ENDPOINTS)
async def test_admin_get_anonymous_rejected(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("path", ADMIN_GET_ENDPOINTS)
async def test_admin_get_teacher_forbidden(teacher_client: AsyncClient, path: str) -> None:
    assert (await teacher_client.get(path)).status_code == 403


@pytest.mark.parametrize("path", ADMIN_GET_ENDPOINTS)
async def test_admin_get_student_forbidden(student_client: AsyncClient, path: str) -> None:
    assert (await student_client.get(path)).status_code == 403


@pytest.mark.parametrize("path,data", ADMIN_POST_ENDPOINTS)
async def test_admin_post_anonymous_rejected(client: AsyncClient, path: str, data: dict) -> None:
    resp = await client.post(path, data=data, follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.parametrize("path,data", ADMIN_POST_ENDPOINTS)
async def test_admin_post_teacher_forbidden(
    teacher_client: AsyncClient, path: str, data: dict
) -> None:
    resp = await teacher_client.post(path, data=data, follow_redirects=False)
    assert resp.status_code == 403


@pytest.mark.parametrize("path,data", ADMIN_POST_ENDPOINTS)
async def test_admin_post_student_forbidden(
    student_client: AsyncClient, path: str, data: dict
) -> None:
    resp = await student_client.post(path, data=data, follow_redirects=False)
    assert resp.status_code == 403


# ── Read-only pages render for admin ─────────────────────────────────────────


@pytest.mark.parametrize("path", ADMIN_GET_ENDPOINTS)
async def test_admin_pages_render_for_admin(admin_client: AsyncClient, path: str) -> None:
    assert (await admin_client.get(path)).status_code == 200


# ── teachers/create ───────────────────────────────────────────────────────────


async def test_create_teacher_creates_active_teacher_with_hash_and_audit(
    admin_client: AsyncClient, admin, db
) -> None:
    password = "teacherPass123"
    resp = await admin_client.post(
        "/admin/teachers/create",
        data={"username": "newteacher", "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"

    user = (await db.execute(select(User).where(User.username == "newteacher"))).scalar_one()
    assert user.role == UserRole.TEACHER
    assert user.is_active is True
    assert user.password_hash != password
    assert bcrypt.checkpw(password.encode(), user.password_hash.encode())

    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "create_teacher"))
    ).scalar_one()
    assert log.actor_id == admin.id
    assert log.actor_username == admin.username
    assert log.detail.get("new_username") == "newteacher"


async def test_create_teacher_trims_username(admin_client: AsyncClient, db) -> None:
    resp = await admin_client.post(
        "/admin/teachers/create",
        data={"username": "  spaced  ", "password": "longenough1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    user = (await db.execute(select(User).where(User.username == "spaced"))).scalar_one()
    assert user.role == UserRole.TEACHER


async def test_create_teacher_short_password_rejected(admin_client: AsyncClient, db) -> None:
    resp = await admin_client.post(
        "/admin/teachers/create",
        data={"username": "shorty", "password": "short"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    count = (
        await db.execute(select(func.count(User.id)).where(User.username == "shorty"))
    ).scalar_one()
    assert count == 0


async def test_create_teacher_duplicate_username_rejected(
    admin_client: AsyncClient, make_user, db
) -> None:
    await make_user(role=UserRole.TEACHER, username="dupe")
    resp = await admin_client.post(
        "/admin/teachers/create",
        data={"username": "dupe", "password": "longenough1"},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    # Still exactly one user with that name (no second row, no overwrite).
    count = (
        await db.execute(select(func.count(User.id)).where(User.username == "dupe"))
    ).scalar_one()
    assert count == 1


async def test_create_teacher_duplicate_writes_no_audit(
    admin_client: AsyncClient, make_user, db
) -> None:
    await make_user(role=UserRole.TEACHER, username="dupe")
    await admin_client.post(
        "/admin/teachers/create",
        data={"username": "dupe", "password": "longenough1"},
        follow_redirects=False,
    )
    count = (
        await db.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "create_teacher"))
    ).scalar_one()
    assert count == 0


# ── users/{id}/toggle-active ──────────────────────────────────────────────────


async def test_toggle_active_flips_and_audits(
    admin_client: AsyncClient, admin, make_user, db
) -> None:
    target = await make_user(role=UserRole.TEACHER, username="victim", is_active=True)

    resp = await admin_client.post(
        f"/admin/users/{target.id}/toggle-active", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"

    fresh = await db.get(User, target.id)
    await db.refresh(fresh)
    assert fresh.is_active is False  # flipped from True

    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "toggle_user_active"))
    ).scalar_one()
    assert log.actor_id == admin.id
    assert log.actor_username == admin.username
    assert log.detail.get("target_user_id") == target.id
    assert log.detail.get("is_active") is False


async def test_toggle_active_reactivates(admin_client: AsyncClient, make_user, db) -> None:
    target = await make_user(role=UserRole.TEACHER, username="victim", is_active=False)
    resp = await admin_client.post(
        f"/admin/users/{target.id}/toggle-active", follow_redirects=False
    )
    assert resp.status_code == 303
    fresh = await db.get(User, target.id)
    await db.refresh(fresh)
    assert fresh.is_active is True


async def test_toggle_active_missing_user_returns_404(admin_client: AsyncClient, db) -> None:
    resp = await admin_client.post("/admin/users/999999/toggle-active", follow_redirects=False)
    assert resp.status_code == 404
    # No audit entry for a no-op.
    count = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.action == "toggle_user_active")
        )
    ).scalar_one()
    assert count == 0


async def test_toggle_active_self_rejected(admin_client: AsyncClient, admin, db) -> None:
    """An admin cannot deactivate their own account (lockout guard)."""
    resp = await admin_client.post(f"/admin/users/{admin.id}/toggle-active", follow_redirects=False)
    assert resp.status_code == 400

    fresh = await db.get(User, admin.id)
    await db.refresh(fresh)
    assert fresh.is_active is True  # unchanged
    count = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.action == "toggle_user_active")
        )
    ).scalar_one()
    assert count == 0
