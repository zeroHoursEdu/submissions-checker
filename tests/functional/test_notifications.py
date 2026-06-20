"""Functional coverage for the in-app NOTIFICATIONS routes.

Focus: authentication, per-user ownership (one user must never see or mutate
another user's notifications), and the read-state side effects in the DB.
Behavior is asserted against what ``api/routes/notifications.py`` actually does.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.notification import Notification
from submissions_checker.db.models.user import User
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


# ── Arrangement helpers ──────────────────────────────────────────────────────


async def _make_notification(
    db, *, user_id: int, title: str = "Hi", is_read: bool = False
) -> Notification:
    notification = Notification(
        user_id=user_id, title=title, body="body text", is_read=is_read
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)
    return notification


# ── Authentication ────────────────────────────────────────────────────────────


async def test_notifications_endpoints_require_auth(client: AsyncClient) -> None:
    """Anonymous requests are rejected with 401 on every notifications endpoint."""
    assert (await client.get("/notifications")).status_code == 401
    assert (await client.get("/notifications/unread-count")).status_code == 401
    assert (await client.post("/notifications/1/read")).status_code == 401
    assert (await client.post("/notifications/read-all")).status_code == 401


# ── Ownership: list / unread-count are scoped to the caller ───────────────────


async def test_list_shows_only_own_notifications(
    client: AsyncClient, make_user, db
) -> None:
    user_a = await make_user(role=UserRole.TEACHER, username="a")
    user_b = await make_user(role=UserRole.TEACHER, username="b")

    await _make_notification(db, user_id=user_a.id, title="A-only-secret")
    await _make_notification(db, user_id=user_b.id, title="B-only-secret")

    authenticate(client, user_a)
    resp = await client.get("/notifications")
    assert resp.status_code == 200
    assert "A-only-secret" in resp.text
    assert "B-only-secret" not in resp.text


async def test_unread_count_counts_only_own(
    client: AsyncClient, make_user, db
) -> None:
    user_a = await make_user(role=UserRole.TEACHER, username="a")
    user_b = await make_user(role=UserRole.TEACHER, username="b")

    # A has 2 unread + 1 read; B has 5 unread.
    await _make_notification(db, user_id=user_a.id)
    await _make_notification(db, user_id=user_a.id)
    await _make_notification(db, user_id=user_a.id, is_read=True)
    for _ in range(5):
        await _make_notification(db, user_id=user_b.id)

    authenticate(client, user_a)
    resp = await client.get("/notifications/unread-count")
    assert resp.status_code == 200
    assert resp.json() == {"count": 2}


# ── Ownership: a user cannot mark ANOTHER user's notification read ─────────────


async def test_cannot_mark_other_users_notification_read(
    client: AsyncClient, make_user, db
) -> None:
    user_a = await make_user(role=UserRole.TEACHER, username="a")
    user_b = await make_user(role=UserRole.TEACHER, username="b")
    b_notification = await _make_notification(db, user_id=user_b.id)

    authenticate(client, user_a)
    # Handler raises HTTPException(404) when the notification belongs to someone
    # else (it treats it as non-existent for the caller).
    resp = await client.post(
        f"/notifications/{b_notification.id}/read", follow_redirects=False
    )
    assert resp.status_code == 404

    # B's notification must remain unread in the DB.
    fresh = await db.get(Notification, b_notification.id)
    await db.refresh(fresh)
    assert fresh.is_read is False
    assert fresh.read_at is None


async def test_mark_read_missing_notification_returns_404(
    client: AsyncClient, teacher: User
) -> None:
    authenticate(client, teacher)
    resp = await client.post("/notifications/999999/read", follow_redirects=False)
    assert resp.status_code == 404


# ── Marking read flips the DB state and decrements unread-count ───────────────


async def test_mark_read_flips_state_and_decrements_count(
    client: AsyncClient, make_user, db
) -> None:
    user_a = await make_user(role=UserRole.TEACHER, username="a")
    n1 = await _make_notification(db, user_id=user_a.id)
    await _make_notification(db, user_id=user_a.id)  # second unread

    authenticate(client, user_a)
    assert (await client.get("/notifications/unread-count")).json() == {"count": 2}

    resp = await client.post(f"/notifications/{n1.id}/read", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"

    fresh = await db.get(Notification, n1.id)
    await db.refresh(fresh)
    assert fresh.is_read is True
    assert fresh.read_at is not None

    assert (await client.get("/notifications/unread-count")).json() == {"count": 1}


# ── read-all only affects the caller's own notifications ──────────────────────


async def test_read_all_only_affects_caller(
    client: AsyncClient, make_user, db
) -> None:
    user_a = await make_user(role=UserRole.TEACHER, username="a")
    user_b = await make_user(role=UserRole.TEACHER, username="b")

    a1 = await _make_notification(db, user_id=user_a.id)
    a2 = await _make_notification(db, user_id=user_a.id)
    b1 = await _make_notification(db, user_id=user_b.id)

    authenticate(client, user_a)
    resp = await client.post("/notifications/read-all", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"

    # All of A's notifications are read.
    for nid in (a1.id, a2.id):
        fresh = await db.get(Notification, nid)
        await db.refresh(fresh)
        assert fresh.is_read is True

    # B's notification is untouched.
    fresh_b = await db.get(Notification, b1.id)
    await db.refresh(fresh_b)
    assert fresh_b.is_read is False

    # A's unread count is now 0.
    assert (await client.get("/notifications/unread-count")).json() == {"count": 0}

    # Sanity: B still has 1 unread.
    b_count = await db.execute(
        select(Notification).where(
            Notification.user_id == user_b.id, Notification.is_read.is_(False)
        )
    )
    assert len(b_count.scalars().all()) == 1
