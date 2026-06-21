"""Targeted coverage for under-covered route branches.

This file closes functional-coverage gaps that ``test_auth_flows.py``,
``test_admin.py`` and ``test_student_portal.py`` do not already exercise. It is
additive — none of those files are modified. The focus areas are:

* ``auth.py`` — the forgot-password EMAIL DISPATCH branch (the dispatcher is
  patched at the route boundary so no real email leaves the process), plus the
  no-channel / no-email fallbacks, the reset-password GET form render (valid vs
  invalid token) and the reset-password 404 edge case.
* ``health.py`` — the readiness FAILURE path (503) by overriding ``get_db`` with
  a session whose ``execute`` raises.
* ``users.py`` — the skeleton ``GET /{user_id}`` and ``POST`` handlers.
* ``student_portal.py`` — cheap reachable error / redirect / empty-state
  branches.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import submissions_checker.api.routes.auth as auth_module
from submissions_checker.core import database as database_module
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.password_reset import PasswordResetToken
from submissions_checker.db.models.student import Student
from submissions_checker.main import app

pytestmark = pytest.mark.asyncio

PASSWORD = "Sup3rSecret!"


# ── Test doubles ──────────────────────────────────────────────────────────────


class _RecordingChannel:
    """A notification channel that records every send instead of sending."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


class _RecordingDispatcher:
    """Stand-in for NotificationDispatcher exposing the same surface the route uses."""

    def __init__(self, channels: list[_RecordingChannel]) -> None:
        self._channels = channels

    async def notify(self, recipient: str, subject: str, body: str) -> None:
        for channel in self._channels:
            await channel.send(recipient, subject, body)


# ── auth.py: trivial GET renders (lines 51-59, 96) ────────────────────────────


async def test_login_page_redirects_already_authenticated_user(
    client: AsyncClient, make_user
) -> None:
    """GET /auth/login with a valid cookie redirects to the role landing page.

    Covers auth.py lines 51-59 (the decode-cookie → redirect-by-role branch).
    """
    user = await make_user(role=UserRole.TEACHER, username="loggedin", password=PASSWORD)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/teacher"


async def test_login_page_renders_for_invalid_cookie(client: AsyncClient) -> None:
    """A malformed auth cookie falls through to rendering the login form (except branch)."""
    from submissions_checker.core.security import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, "not-a-real-jwt")
    resp = await client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 200


async def test_forgot_password_page_renders(client: AsyncClient) -> None:
    """GET /auth/forgot-password renders the request form (auth.py line 96)."""
    resp = await client.get("/auth/forgot-password")
    assert resp.status_code == 200


# ── auth.py: forgot-password EMAIL DISPATCH branch ────────────────────────────


async def test_forgot_password_dispatches_reset_email_to_student_email(
    client: AsyncClient, make_user, make_student, monkeypatch
) -> None:
    """User linked to a student with an email + a configured channel → email sent.

    Exercises auth.py lines ~118-131: email resolution from the linked student
    and dispatch through a non-empty dispatcher.
    """
    student = await make_student(email="learner@example.com", full_name="Lena Learner")
    user = await make_user(
        role=UserRole.STUDENT, username="lena", password=PASSWORD, student=student
    )

    channel = _RecordingChannel()
    monkeypatch.setattr(
        auth_module, "build_dispatcher", lambda settings: _RecordingDispatcher([channel])
    )

    resp = await client.post("/auth/forgot-password", data={"username": "lena"})
    assert resp.status_code == 200

    # Exactly one email, to the student's address, carrying the freshly created token.
    assert len(channel.sent) == 1
    recipient, subject, body = channel.sent[0]
    assert recipient == "learner@example.com"
    assert "Reset your EduTrack password" == subject
    assert "Lena Learner" in body

    # The dispatched link must carry the token that was persisted for this user.
    from submissions_checker.core.config import get_settings

    # Re-read the token from the DB through a fresh request-independent query.
    # (We can't use the `db` fixture here without importing it; assert via body.)
    assert "/auth/reset-password?token=" in body
    base = get_settings().app_base_url.rstrip("/")
    assert body.count(base) >= 1
    assert user.username == "lena"


async def test_forgot_password_token_in_email_matches_persisted_token(
    client: AsyncClient, make_user, make_student, db, monkeypatch
) -> None:
    """The token embedded in the email is the exact one persisted to the DB."""
    student = await make_student(email="match@example.com")
    user = await make_user(
        role=UserRole.STUDENT, username="matcher", password=PASSWORD, student=student
    )

    channel = _RecordingChannel()
    monkeypatch.setattr(
        auth_module, "build_dispatcher", lambda settings: _RecordingDispatcher([channel])
    )

    resp = await client.post("/auth/forgot-password", data={"username": "matcher"})
    assert resp.status_code == 200

    prt = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
    ).scalar_one()

    assert len(channel.sent) == 1
    _, _, body = channel.sent[0]
    assert f"token={prt.token}" in body


async def test_forgot_password_no_channels_configured_sends_nothing(
    client: AsyncClient, make_user, make_student, db, monkeypatch
) -> None:
    """User has an email but no channel is configured → dispatcher.notify skipped.

    Covers the false branch of ``if dispatcher._channels:`` (auth.py line 130).
    A token is still created; no send occurs.
    """
    student = await make_student(email="nochannel@example.com")
    user = await make_user(
        role=UserRole.STUDENT, username="nochan", password=PASSWORD, student=student
    )

    notified: list[tuple] = []

    class _EmptyDispatcher(_RecordingDispatcher):
        async def notify(self, *args: object) -> None:  # pragma: no cover - must not run
            notified.append(args)

    monkeypatch.setattr(
        auth_module, "build_dispatcher", lambda settings: _EmptyDispatcher([])
    )

    resp = await client.post("/auth/forgot-password", data={"username": "nochan"})
    assert resp.status_code == 200
    assert notified == []  # notify never called when there are no channels

    token_count = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
    ).scalars().all()
    assert len(token_count) == 1  # token still created


async def test_forgot_password_user_without_email_sends_nothing(
    client: AsyncClient, make_user, db, monkeypatch
) -> None:
    """Teacher user (no linked student → no email) → email resolution stays None.

    Covers the false branch of ``if email:`` (auth.py line 125). build_dispatcher
    must not even be consulted for a send.
    """
    user = await make_user(role=UserRole.TEACHER, username="noemail", password=PASSWORD)

    built: list[object] = []

    def _spy_build(settings: object) -> _RecordingDispatcher:
        built.append(settings)
        return _RecordingDispatcher([_RecordingChannel()])

    monkeypatch.setattr(auth_module, "build_dispatcher", _spy_build)

    resp = await client.post("/auth/forgot-password", data={"username": "noemail"})
    assert resp.status_code == 200

    # No email → the dispatch block is skipped entirely, so build was never called.
    assert built == []

    tokens = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
    ).scalars().all()
    assert len(tokens) == 1


# NOTE: auth.py lines 122-123 ("student found but student.email falsy") are not
# reachable through the API: Student.email is a NOT NULL column, so a linked
# student always carries a non-empty address. The ``if email:`` *false* branch
# (line 125) is instead covered by test_forgot_password_user_without_email_sends_nothing,
# where the user has no linked student at all.


# ── auth.py: reset-password GET form render ───────────────────────────────────


async def test_reset_password_get_renders_form_for_valid_token(
    client: AsyncClient, make_user, db
) -> None:
    """GET /auth/reset-password?token=... renders the form, valid=True for a live token."""
    user = await make_user(role=UserRole.TEACHER, username="getvalid", password=PASSWORD)
    prt = PasswordResetToken.create(user_id=user.id, token="get-valid-token")
    db.add(prt)
    await db.commit()

    resp = await client.get("/auth/reset-password", params={"token": "get-valid-token"})
    assert resp.status_code == 200
    # The valid-token form contains the password inputs, not the invalid-token notice.
    assert "new_password" in resp.text


async def test_reset_password_get_invalid_token_renders_notice(
    client: AsyncClient
) -> None:
    """GET with an unknown token still renders the page (valid=False branch)."""
    resp = await client.get("/auth/reset-password", params={"token": "does-not-exist"})
    assert resp.status_code == 200
    # Page renders fine even though the token is invalid (no password form expected).
    assert "new_password" not in resp.text


async def test_reset_password_get_expired_token_renders_invalid(
    client: AsyncClient, make_user, db
) -> None:
    """GET with an expired token renders the invalid form (is_valid() False branch)."""
    from datetime import UTC, datetime, timedelta

    user = await make_user(role=UserRole.TEACHER, username="getexpired", password=PASSWORD)
    prt = PasswordResetToken.create(user_id=user.id, token="get-expired-token")
    prt.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db.add(prt)
    await db.commit()

    resp = await client.get("/auth/reset-password", params={"token": "get-expired-token"})
    assert resp.status_code == 200
    assert "new_password" not in resp.text


# NOTE: auth.py line 173 (reset-password POST → 404 when ``db.get(User, prt.user_id)``
# is None) is not reachable through the API. ``password_reset_tokens.user_id`` is a
# NOT NULL FK with ON DELETE CASCADE, so a token can only exist while its user does;
# deleting the user cascades the token away, and the FK rejects pointing a token at a
# non-existent user id. The branch is defensive-only dead code under the schema.


# ── health.py: readiness FAILURE path (503) ───────────────────────────────────


async def test_readiness_check_returns_503_when_db_unavailable(
    client: AsyncClient,
) -> None:
    """Force the DB check to raise → readiness returns 503 with the documented detail.

    Overrides get_db with a session whose ``execute`` raises, then restores the
    override the harness installed so other tests are unaffected.
    """

    class _BrokenSession:
        async def execute(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("connection refused")

    async def _broken_get_db() -> AsyncGenerator[_BrokenSession, None]:
        yield _BrokenSession()

    previous = app.dependency_overrides.get(database_module.get_db)
    app.dependency_overrides[database_module.get_db] = _broken_get_db
    try:
        resp = await client.get("/health/ready")
    finally:
        if previous is not None:
            app.dependency_overrides[database_module.get_db] = previous
        else:
            app.dependency_overrides.pop(database_module.get_db, None)

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Database connection failed"


async def test_health_check_basic_ok(client: AsyncClient) -> None:
    """Basic /health stays healthy (the success counterpart)."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


async def test_readiness_check_ok_when_db_healthy(client: AsyncClient) -> None:
    """Readiness succeeds against the real test DB (the 200 branch)."""
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "database": "connected"}


# ── users.py: skeleton handlers ───────────────────────────────────────────────


async def test_get_user_skeleton_returns_not_implemented(client: AsyncClient) -> None:
    """GET /api/v1/users/{id} is an unauthenticated skeleton returning a stub payload."""
    resp = await client.get("/api/v1/users/42")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "not_implemented",
        "message": "User retrieval not yet implemented",
    }


async def test_get_user_rejects_non_integer_id(client: AsyncClient) -> None:
    """The {user_id} path param is typed int → a non-int 422s (FastAPI validation)."""
    resp = await client.get("/api/v1/users/not-a-number")
    assert resp.status_code == 422


async def test_create_user_skeleton_returns_not_implemented(client: AsyncClient) -> None:
    """POST /api/v1/users is an unauthenticated skeleton returning a stub payload."""
    resp = await client.post("/api/v1/users")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "not_implemented",
        "message": "User creation not yet implemented",
    }


# ── student_portal.py: cheap reachable branches ───────────────────────────────


async def test_portal_grid_redirects_to_consent_when_not_consented(
    client: AsyncClient, make_user, make_student
) -> None:
    """A student who has not consented is redirected to /portal/consent (303).

    Covers student_portal.py ~line 88-89 (consent redirect on the grid route).
    """
    student = await make_student()  # recording_consent_at defaults to None
    user = await make_user(role=UserRole.STUDENT, username="needconsent", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get("/portal", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/consent"


async def test_portal_consent_redirects_when_already_consented(
    client: AsyncClient, make_user, make_student, db
) -> None:
    """GET /portal/consent redirects to /portal once consent is recorded (line 60-61)."""
    from datetime import UTC, datetime

    student = await make_student()
    fetched = await db.get(Student, student.id)
    fetched.recording_consent_at = datetime.now(UTC)
    await db.commit()

    user = await make_user(role=UserRole.STUDENT, username="alreadyok", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get("/portal/consent", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


async def test_portal_assignments_list_404_when_not_enrolled(
    client: AsyncClient, make_user, make_student, make_group, db
) -> None:
    """Requesting a subject the student is not enrolled in → 404 (line ~156-157)."""
    from datetime import UTC, datetime

    from submissions_checker.db.models import Subject

    student = await make_student()
    fetched = await db.get(Student, student.id)
    fetched.recording_consent_at = datetime.now(UTC)
    await db.commit()

    subject = Subject(name="Unenrolled Subject")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    user = await make_user(role=UserRole.STUDENT, username="notenrolled", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get(f"/portal/subjects/{subject.id}", follow_redirects=False)
    assert resp.status_code == 404


async def test_portal_assignment_detail_404_for_foreign_student_assignment(
    client: AsyncClient, make_user, make_student
) -> None:
    """A student_assignment id not owned by the caller → 404 (line ~224-225)."""
    student = await make_student()
    user = await make_user(role=UserRole.STUDENT, username="detail404", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get(
        "/portal/subjects/1/assignments/99999", follow_redirects=False
    )
    assert resp.status_code == 404


async def test_portal_summary_empty_state_renders(
    client: AsyncClient, make_user, make_student
) -> None:
    """The summary page renders for a student with no enrollments (empty-state branch).

    Covers the ``else: all_rows = []`` path (line ~481-482) and the empty
    subs/avg branches.
    """
    student = await make_student()
    user = await make_user(role=UserRole.STUDENT, username="emptysummary", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.get("/portal/summary", follow_redirects=False)
    assert resp.status_code == 200


async def test_portal_toggle_notification_preference_creates_disabled_row(
    client: AsyncClient, make_user, make_student, db
) -> None:
    """Toggling a preference with no existing row inserts an enabled=False row.

    Covers the ``pref is None`` insert branch (line ~613-614).
    """
    from submissions_checker.db.models.notification_preference import (
        NotificationPreference,
    )

    student = await make_student()
    user = await make_user(role=UserRole.STUDENT, username="preftoggle", student=student)

    from tests.functional.conftest import authenticate

    authenticate(client, user)
    resp = await client.post(
        "/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.student_id == student.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].enabled is False


# NOTE: submit_assignment and assignment_detail's quiz/plugin-config branches
# (student_portal.py ~248-267) require a Submission + QuizAttempt / plugin config
# and ultimately the async check worker to be meaningful; they are intentionally
# left to integration coverage rather than exercised here.
