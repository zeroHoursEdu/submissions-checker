"""Login / password-reset flow coverage.

Complements ``test_auth_security.py`` (which proves the JWT/role authorization
*matrix*). This file exercises the credential and password-reset *flows*:
``/auth/login``, ``/auth/logout``, ``/auth/forgot-password`` and
``/auth/reset-password`` — asserting exact status codes, the auth cookie, and
the real DB side-effects (UserLogin rows, PasswordResetToken creation/consumption,
and that the stored password is a bcrypt hash rather than plaintext).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from submissions_checker.core.security import COOKIE_NAME, decode_access_token
from submissions_checker.db.models.enums import UserRole
from submissions_checker.db.models.password_reset import PasswordResetToken
from submissions_checker.db.models.user import User
from submissions_checker.db.models.user_login import UserLogin

pytestmark = pytest.mark.asyncio

PASSWORD = "Sup3rSecret!"


# ── Login: success ────────────────────────────────────────────────────────────


async def test_login_success_sets_valid_jwt_cookie(client: AsyncClient, make_user) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)

    resp = await client.post(
        "/auth/login",
        data={"username": "alice", "password": PASSWORD},
        follow_redirects=False,
    )

    # Successful login redirects (303) to the role landing page.
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher"

    token = resp.cookies.get(COOKIE_NAME)
    assert token, "login must set the access_token cookie"

    payload = decode_access_token(token)
    assert payload["sub"] == str(user.id)
    assert payload["username"] == "alice"
    assert payload["role"] == UserRole.TEACHER.value


async def test_login_redirects_student_to_portal(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.STUDENT, username="pupil", password=PASSWORD)
    resp = await client.post(
        "/auth/login",
        data={"username": "pupil", "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


async def test_login_redirects_admin_to_admin_dashboard(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.ADMIN, username="root", password=PASSWORD)
    resp = await client.post(
        "/auth/login",
        data={"username": "root", "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"


async def test_login_records_user_login_row(client: AsyncClient, make_user, db) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)

    await client.post(
        "/auth/login",
        data={"username": "alice", "password": PASSWORD},
        follow_redirects=False,
    )

    rows = (await db.execute(select(UserLogin).where(UserLogin.user_id == user.id))).scalars().all()
    assert len(rows) == 1


async def test_password_is_stored_as_bcrypt_hash_not_plaintext(make_user, db) -> None:
    """The factory uses the app's hash_password; confirm it is a verifiable bcrypt
    hash and never the plaintext, which is what login relies on."""
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    fresh = await db.get(User, user.id)
    assert fresh is not None
    assert fresh.password_hash != PASSWORD
    assert fresh.password_hash.startswith("$2")  # bcrypt prefix
    assert bcrypt.checkpw(PASSWORD.encode(), fresh.password_hash.encode())


# ── Login: rejection ──────────────────────────────────────────────────────────


async def test_login_wrong_password_rejected_no_cookie(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)

    resp = await client.post(
        "/auth/login",
        data={"username": "alice", "password": "wrong-password"},
        follow_redirects=False,
    )

    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies
    assert "Invalid username or password" in resp.text


async def test_login_unknown_user_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/login",
        data={"username": "ghost", "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies


async def test_login_inactive_user_rejected(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD, is_active=False)
    resp = await client.post(
        "/auth/login",
        data={"username": "alice", "password": PASSWORD},
        follow_redirects=False,
    )
    # Inactive users are filtered out by the query, so they look like a bad login.
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies


async def test_failed_login_records_no_user_login_row(client: AsyncClient, make_user, db) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    await client.post(
        "/auth/login",
        data={"username": "alice", "password": "nope"},
        follow_redirects=False,
    )
    count = (
        await db.execute(select(func.count(UserLogin.id)).where(UserLogin.user_id == user.id))
    ).scalar_one()
    assert count == 0


# ── Logout ────────────────────────────────────────────────────────────────────


async def test_logout_clears_cookie(client: AsyncClient, make_user, login) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    login(client, user)

    resp = await client.post("/auth/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    # delete_cookie emits a Set-Cookie that expires the cookie.
    set_cookie = resp.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("expires=" in set_cookie.lower())


# ── Forgot password ───────────────────────────────────────────────────────────


async def test_forgot_password_existing_user_creates_token(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)

    resp = await client.post("/auth/forgot-password", data={"username": "alice"})
    assert resp.status_code == 200

    tokens = (
        (await db.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(tokens) == 1
    assert tokens[0].used is False
    assert tokens[0].is_valid()


async def test_forgot_password_unknown_user_does_not_leak(
    client: AsyncClient, make_user, db
) -> None:
    """An unknown username must yield the same response as a known one and create
    no token (no enumeration oracle)."""
    await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)

    known = await client.post("/auth/forgot-password", data={"username": "alice"})
    unknown = await client.post("/auth/forgot-password", data={"username": "ghost"})

    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text  # indistinguishable response bodies

    total_tokens = (await db.execute(select(func.count(PasswordResetToken.id)))).scalar_one()
    assert total_tokens == 1  # only the real user got one


async def test_forgot_password_inactive_user_creates_no_token(
    client: AsyncClient, make_user, db
) -> None:
    await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD, is_active=False)
    resp = await client.post("/auth/forgot-password", data={"username": "alice"})
    assert resp.status_code == 200
    count = (await db.execute(select(func.count(PasswordResetToken.id)))).scalar_one()
    assert count == 0


# ── Reset password ────────────────────────────────────────────────────────────


async def _make_reset_token(db, user_id: int, used: bool = False) -> str:
    token_str = f"reset-token-{user_id}-{'used' if used else 'fresh'}"
    prt = PasswordResetToken.create(user_id=user_id, token=token_str)
    prt.used = used
    db.add(prt)
    await db.commit()
    return token_str


async def test_reset_password_valid_token_updates_hash_and_consumes_token(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    old_hash = user.password_hash
    token_str = await _make_reset_token(db, user.id)

    new_pw = "BrandNewPass1"
    resp = await client.post(
        "/auth/reset-password",
        data={
            "token": token_str,
            "new_password": new_pw,
            "confirm_password": new_pw,
        },
    )
    assert resp.status_code == 200

    # Re-fetch from a fresh session to see the committed state.
    fresh = await db.get(User, user.id)
    await db.refresh(fresh)
    assert fresh.password_hash != old_hash
    assert bcrypt.checkpw(new_pw.encode(), fresh.password_hash.encode())

    prt = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.token == token_str))
    ).scalar_one()
    await db.refresh(prt)
    assert prt.used is True
    assert prt.is_valid() is False  # single-use: now consumed


async def test_reset_password_consumed_token_cannot_be_reused(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    token_str = await _make_reset_token(db, user.id)
    new_pw = "BrandNewPass1"

    first = await client.post(
        "/auth/reset-password",
        data={"token": token_str, "new_password": new_pw, "confirm_password": new_pw},
    )
    assert first.status_code == 200

    second_pw = "SecondAttempt9"
    second = await client.post(
        "/auth/reset-password",
        data={
            "token": token_str,
            "new_password": second_pw,
            "confirm_password": second_pw,
        },
    )
    assert second.status_code == 400  # token already used → rejected

    fresh = await db.get(User, user.id)
    await db.refresh(fresh)
    # The second (rejected) attempt must NOT have changed the password.
    assert bcrypt.checkpw(new_pw.encode(), fresh.password_hash.encode())
    assert not bcrypt.checkpw(second_pw.encode(), fresh.password_hash.encode())


async def test_reset_password_unknown_token_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/reset-password",
        data={
            "token": "does-not-exist",
            "new_password": "BrandNewPass1",
            "confirm_password": "BrandNewPass1",
        },
    )
    assert resp.status_code == 400


async def test_reset_password_expired_token_rejected(client: AsyncClient, make_user, db) -> None:
    from datetime import UTC, datetime, timedelta

    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    prt = PasswordResetToken(
        user_id=user.id,
        token="expired-token",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        used=False,
    )
    db.add(prt)
    await db.commit()

    old_hash = user.password_hash
    resp = await client.post(
        "/auth/reset-password",
        data={
            "token": "expired-token",
            "new_password": "BrandNewPass1",
            "confirm_password": "BrandNewPass1",
        },
    )
    assert resp.status_code == 400
    fresh = await db.get(User, user.id)
    await db.refresh(fresh)
    assert fresh.password_hash == old_hash  # unchanged


async def test_reset_password_mismatch_rejected_with_422(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    token_str = await _make_reset_token(db, user.id)

    resp = await client.post(
        "/auth/reset-password",
        data={
            "token": token_str,
            "new_password": "BrandNewPass1",
            "confirm_password": "DifferentPass1",
        },
    )
    assert resp.status_code == 422
    # Token must remain unused since the change never applied.
    prt = (
        await db.execute(select(PasswordResetToken).where(PasswordResetToken.token == token_str))
    ).scalar_one()
    await db.refresh(prt)
    assert prt.used is False


async def test_reset_password_too_short_rejected_with_422(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    token_str = await _make_reset_token(db, user.id)

    resp = await client.post(
        "/auth/reset-password",
        data={"token": token_str, "new_password": "short", "confirm_password": "short"},
    )
    assert resp.status_code == 422


# ── Change password (logged-in) ───────────────────────────────────────────────


async def test_change_password_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/auth/change-password")).status_code == 401
    resp = await client.post(
        "/auth/change-password",
        data={"current_password": "x", "new_password": "y" * 8, "confirm_password": "y" * 8},
    )
    assert resp.status_code == 401


async def test_change_password_updates_hash_and_old_password_stops_working(
    client: AsyncClient, make_user, login, db
) -> None:
    user = await make_user(role=UserRole.STUDENT, username="carol", password=PASSWORD)
    login(client, user)
    resp = await client.post(
        "/auth/change-password",
        data={
            "current_password": PASSWORD,
            "new_password": "N3wPassw0rd!",
            "confirm_password": "N3wPassw0rd!",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(user)
    assert bcrypt.checkpw(b"N3wPassw0rd!", user.password_hash.encode())
    assert not bcrypt.checkpw(PASSWORD.encode(), user.password_hash.encode())


@pytest.mark.parametrize(
    "current,new,confirm",
    [
        ("wrong-current", "N3wPassw0rd!", "N3wPassw0rd!"),
        (PASSWORD, "short", "short"),
        (PASSWORD, "N3wPassw0rd!", "different!!"),
        (PASSWORD, PASSWORD, PASSWORD),
    ],
)
async def test_change_password_rejects_bad_input_with_422(
    client: AsyncClient, make_user, login, db, current: str, new: str, confirm: str
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="dave", password=PASSWORD)
    login(client, user)
    resp = await client.post(
        "/auth/change-password",
        data={"current_password": current, "new_password": new, "confirm_password": confirm},
    )
    assert resp.status_code == 422
    await db.refresh(user)
    assert bcrypt.checkpw(PASSWORD.encode(), user.password_hash.encode())


# ── Throttling ────────────────────────────────────────────────────────────────


async def test_login_is_throttled_after_repeated_failures(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    from submissions_checker.core import rate_limit

    monkeypatch.setattr(rate_limit, "_login_limiter", rate_limit.SlidingWindowLimiter(10, 900))
    await make_user(role=UserRole.TEACHER, username="erin", password=PASSWORD)
    for _ in range(10):
        r = await client.post("/auth/login", data={"username": "erin", "password": "nope"})
        assert r.status_code == 401
    r = await client.post("/auth/login", data={"username": "erin", "password": "nope"})
    assert r.status_code == 429
    # Even the right password is refused while blocked.
    r = await client.post("/auth/login", data={"username": "erin", "password": PASSWORD})
    assert r.status_code == 429


async def test_successful_login_resets_the_counter(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    from submissions_checker.core import rate_limit

    monkeypatch.setattr(rate_limit, "_login_limiter", rate_limit.SlidingWindowLimiter(3, 900))
    await make_user(role=UserRole.TEACHER, username="fay", password=PASSWORD)
    for _ in range(2):
        await client.post("/auth/login", data={"username": "fay", "password": "nope"})
    ok = await client.post(
        "/auth/login", data={"username": "fay", "password": PASSWORD}, follow_redirects=False
    )
    assert ok.status_code == 303
    client.cookies.clear()
    for _ in range(2):
        r = await client.post("/auth/login", data={"username": "fay", "password": "nope"})
        assert r.status_code == 401


async def test_forgot_password_is_throttled_per_client(client: AsyncClient, monkeypatch) -> None:
    from submissions_checker.core import rate_limit

    monkeypatch.setattr(rate_limit, "_login_limiter", rate_limit.SlidingWindowLimiter(2, 900))
    for _ in range(2):
        assert (
            await client.post("/auth/forgot-password", data={"username": "nobody"})
        ).status_code == 200
    assert (
        await client.post("/auth/forgot-password", data={"username": "nobody"})
    ).status_code == 429


# ── Login hardening ──────────────────────────────────────────────────────────


async def test_login_with_overlong_password_is_an_ordinary_failure(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    """bcrypt refuses >72 bytes; that must be a 401 that costs throttle budget, not a 500
    that bypasses the counter."""
    from submissions_checker.core import rate_limit

    monkeypatch.setattr(rate_limit, "_login_limiter", rate_limit.SlidingWindowLimiter(2, 900))
    await make_user(role=UserRole.TEACHER, username="gus", password=PASSWORD)
    for _ in range(2):
        r = await client.post("/auth/login", data={"username": "gus", "password": "x" * 100})
        assert r.status_code == 401
    r = await client.post("/auth/login", data={"username": "gus", "password": PASSWORD})
    assert r.status_code == 429


async def test_password_spraying_is_throttled_per_client(
    client: AsyncClient, make_user, monkeypatch
) -> None:
    """One client trying one password against many usernames hits a per-IP budget."""
    from submissions_checker.core import rate_limit

    monkeypatch.setattr(rate_limit, "_login_limiter", rate_limit.SlidingWindowLimiter(10, 900))
    monkeypatch.setattr(rate_limit, "_login_ip_limiter", rate_limit.SlidingWindowLimiter(3, 900))
    await make_user(role=UserRole.TEACHER, username="hal", password=PASSWORD)
    for name in ("u1", "u2", "u3"):
        r = await client.post("/auth/login", data={"username": name, "password": "guess"})
        assert r.status_code == 401
    r = await client.post("/auth/login", data={"username": "hal", "password": PASSWORD})
    assert r.status_code == 429


async def test_unknown_username_still_pays_for_a_hash_check(
    client: AsyncClient, monkeypatch
) -> None:
    """Without this, response time reveals whether a username exists."""
    from submissions_checker.api.routes import auth as auth_module

    calls: list[str] = []
    real = auth_module.verify_password

    def spy(plain: str, hashed: str) -> bool:
        calls.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    r = await client.post("/auth/login", data={"username": "ghost", "password": "whatever"})
    assert r.status_code == 401
    assert len(calls) == 1


async def test_change_password_rejects_overlong_new_password(
    client: AsyncClient, make_user, login
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="ida", password=PASSWORD)
    login(client, user)
    resp = await client.post(
        "/auth/change-password",
        data={
            "current_password": PASSWORD,
            "new_password": "n" * 73,
            "confirm_password": "n" * 73,
        },
    )
    assert resp.status_code == 422


async def test_reset_password_rejects_overlong_new_password(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="jon", password=PASSWORD)
    db.add(PasswordResetToken.create(user_id=user.id, token="tok-long"))
    await db.commit()
    resp = await client.post(
        "/auth/reset-password",
        data={"token": "tok-long", "new_password": "n" * 73, "confirm_password": "n" * 73},
    )
    assert resp.status_code == 422


# ── Sessions end when the password changes ───────────────────────────────────


def _token_issued_at(user: User, issued_at: datetime) -> str:
    """A cookie exactly as login would have minted it at *issued_at*."""
    from jose import jwt

    from submissions_checker.core.config import get_settings
    from submissions_checker.core.security import JWT_ALGORITHM

    return jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.value,
            "iat": int(issued_at.timestamp()),
            "exp": issued_at + timedelta(hours=8),
        },
        get_settings().secret_key,
        algorithm=JWT_ALGORITHM,
    )


async def test_session_issued_before_password_change_is_refused(
    client: AsyncClient, make_user, db
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="kim", password=PASSWORD)
    old = _token_issued_at(user, datetime.now(UTC) - timedelta(minutes=5))
    client.cookies.set(COOKIE_NAME, old)
    assert (await client.get("/teacher")).status_code == 200

    user.password_changed_at = datetime.now(UTC)
    await db.commit()
    assert (await client.get("/teacher")).status_code == 401


async def test_session_without_issue_time_survives_a_password_change(
    client: AsyncClient, make_user, db
) -> None:
    """Cookies minted before `iat` existed keep working until they expire; the change
    must not log everyone out on deploy."""
    from jose import jwt

    from submissions_checker.core.config import get_settings
    from submissions_checker.core.security import JWT_ALGORITHM

    user = await make_user(role=UserRole.TEACHER, username="lee", password=PASSWORD)
    user.password_changed_at = datetime.now(UTC)
    await db.commit()
    legacy = jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.value,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        get_settings().secret_key,
        algorithm=JWT_ALGORITHM,
    )
    client.cookies.set(COOKIE_NAME, legacy)
    assert (await client.get("/teacher")).status_code == 200


async def test_change_password_ends_other_sessions_and_keeps_this_one(
    client: AsyncClient, make_user
) -> None:
    user = await make_user(role=UserRole.TEACHER, username="mia", password=PASSWORD)
    old = _token_issued_at(user, datetime.now(UTC) - timedelta(minutes=5))
    client.cookies.set(COOKIE_NAME, old)

    resp = await client.post(
        "/auth/change-password",
        data={
            "current_password": PASSWORD,
            "new_password": "N3wPassw0rd!",
            "confirm_password": "N3wPassw0rd!",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    fresh = resp.cookies.get(COOKIE_NAME)
    assert fresh and fresh != old

    # The response also planted the fresh cookie in the jar; test each cookie alone.
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, old)
    assert (await client.get("/teacher")).status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, fresh)
    assert (await client.get("/teacher")).status_code == 200


async def test_reset_password_ends_existing_sessions(client: AsyncClient, make_user, db) -> None:
    user = await make_user(role=UserRole.TEACHER, username="ned", password=PASSWORD)
    old = _token_issued_at(user, datetime.now(UTC) - timedelta(minutes=5))
    db.add(PasswordResetToken.create(user_id=user.id, token="tok-ned"))
    await db.commit()

    resp = await client.post(
        "/auth/reset-password",
        data={
            "token": "tok-ned",
            "new_password": "N3wPassw0rd!",
            "confirm_password": "N3wPassw0rd!",
        },
    )
    assert resp.status_code == 200
    client.cookies.set(COOKIE_NAME, old)
    assert (await client.get("/teacher")).status_code == 401
