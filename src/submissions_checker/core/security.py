"""Security utilities: webhook validation, password hashing, and JWT auth."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import jwt

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

COOKIE_NAME = "access_token"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 8


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(user_id: int, username: str, role: str) -> str:
    """Create HS256 JWT. Claims: sub (user_id), username, role, exp."""
    expire = datetime.now(UTC) + timedelta(hours=JWT_EXPIRY_HOURS)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate JWT. Raises jose.JWTError on failure."""
    return jwt.decode(token, get_settings().secret_key, algorithms=[JWT_ALGORITHM])  # type: ignore[no-any-return]


# ── GitHub webhook validation ─────────────────────────────────────────────────

def verify_github_signature(payload: bytes, signature_header: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    settings = get_settings()

    if not signature_header:
        logger.warning("github_webhook_missing_signature")
        return False

    if not signature_header.startswith("sha256="):
        logger.warning("github_webhook_invalid_signature_format")
        return False

    expected_signature = signature_header[7:]
    secret = settings.github_webhook_secret.encode("utf-8")
    computed_signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()

    is_valid = hmac.compare_digest(computed_signature, expected_signature)
    if not is_valid:
        logger.warning("github_webhook_signature_mismatch")

    return is_valid


def create_webhook_signature(payload: bytes) -> str:
    """Create GitHub-style webhook signature for testing."""
    settings = get_settings()
    secret = settings.github_webhook_secret.encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"sha256={signature}"
