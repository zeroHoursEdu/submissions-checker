"""Security utilities: password hashing and JWT auth."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

COOKIE_NAME = "access_token"
JWT_ALGORITHM = "HS256"
# Base class of every PyJWT decode failure (signature, expiry, malformed, wrong alg).
TokenError = jwt.PyJWTError
JWT_EXPIRY_HOURS = 8


# ── Password hashing ──────────────────────────────────────────────────────────

# bcrypt hashes at most 72 bytes; bcrypt 5 raises on longer input instead of silently
# truncating. Callers validate before hashing and treat longer input as a wrong password.
MAX_PASSWORD_BYTES = 72

_DUMMY_HASH: str | None = None


def password_too_long(plain: str) -> bool:
    return len(plain.encode()) > MAX_PASSWORD_BYTES


def hash_password(plain: str) -> str:
    if password_too_long(plain):
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if password_too_long(plain):
        return False
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def dummy_password_hash() -> str:
    """A real bcrypt hash to verify against when the username does not exist.

    Without it, a login for an unknown username returns noticeably faster than one for a
    known username with a wrong password, which turns the login form into a username
    oracle. Computed once per process.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("no-such-user")
    return _DUMMY_HASH


# ── Bearer tokens at rest ─────────────────────────────────────────────────────


def hash_token(raw: str) -> str:
    """SHA-256 hex of a high-entropy bearer token (reset / feedback links).

    Plain SHA-256 is enough here: the tokens are 32+ random bytes, so there is nothing
    to guess and no need for a slow hash. Storing only the hash means a database read
    cannot produce a working link.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


# ── JWT ───────────────────────────────────────────────────────────────────────


def create_access_token(
    user_id: int, username: str, role: str, *, issued_at: datetime | None = None
) -> str:
    """Create HS256 JWT. Claims: sub (user_id), username, role, iat, exp.

    ``iat`` is compared against ``User.password_changed_at`` on every request, so a
    password change ends every session that predates it.
    """
    now = issued_at or datetime.now(UTC)
    expire = now + timedelta(hours=JWT_EXPIRY_HOURS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": expire,
    }
    token: str = jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALGORITHM)
    return token


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate JWT. Raises TokenError on failure."""
    payload: dict[str, Any] = jwt.decode(
        token, get_settings().secret_key, algorithms=[JWT_ALGORITHM]
    )
    return payload


def issued_before_password_change(payload: dict[str, Any], changed_at: datetime | None) -> bool:
    """Whether a decoded token predates the user's last password change.

    Tokens without ``iat`` (minted before the claim existed) are never refused on this
    ground: they age out on their own within JWT_EXPIRY_HOURS.
    """
    iat = payload.get("iat")
    if changed_at is None or not isinstance(iat, int):
        return False
    return iat < int(changed_at.timestamp())
