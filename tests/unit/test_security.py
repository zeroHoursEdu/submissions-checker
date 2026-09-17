"""Unit tests for core.security — password hashing and JWT round-trips.

No DB, no network. Settings are read from the project .env (which provides a
valid SECRET_KEY); expired/wrong-secret tokens are crafted with PyJWT directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from jwt import PyJWTError as JWTError

from submissions_checker.core import security
from submissions_checker.core.config import get_settings

# ── Password hashing ──────────────────────────────────────────────────────────


def test_hash_password_verifies_correct_password() -> None:
    h = security.hash_password("hunter2")
    assert security.verify_password("hunter2", h) is True


def test_verify_rejects_wrong_password() -> None:
    h = security.hash_password("hunter2")
    assert security.verify_password("hunter3", h) is False


def test_hash_is_not_plaintext() -> None:
    h = security.hash_password("hunter2")
    assert "hunter2" not in h
    assert h.startswith("$2")  # bcrypt prefix


def test_same_password_different_salts_produce_different_hashes() -> None:
    h1 = security.hash_password("samepw")
    h2 = security.hash_password("samepw")
    assert h1 != h2
    # but both verify
    assert security.verify_password("samepw", h1)
    assert security.verify_password("samepw", h2)


def test_verify_rejects_tampered_hash() -> None:
    h = security.hash_password("hunter2")
    # flip the last character of the hash
    last = "A" if h[-1] != "A" else "B"
    tampered = h[:-1] + last
    assert security.verify_password("hunter2", tampered) is False


def test_empty_password_round_trips() -> None:
    h = security.hash_password("")
    assert security.verify_password("", h) is True
    assert security.verify_password("x", h) is False


# ── JWT ───────────────────────────────────────────────────────────────────────


def test_create_and_decode_round_trips_claims() -> None:
    token = security.create_access_token(user_id=42, username="alice", role="TEACHER")
    claims = security.decode_access_token(token)
    assert claims["sub"] == "42"
    assert claims["username"] == "alice"
    assert claims["role"] == "TEACHER"


def test_sub_is_stringified_user_id() -> None:
    token = security.create_access_token(user_id=7, username="bob", role="STUDENT")
    claims = security.decode_access_token(token)
    assert claims["sub"] == "7"
    assert isinstance(claims["sub"], str)


def test_token_expiry_is_in_the_future() -> None:
    before = datetime.now(UTC)
    token = security.create_access_token(user_id=1, username="x", role="STUDENT")
    claims = security.decode_access_token(token)
    exp = datetime.fromtimestamp(claims["exp"], tz=UTC)
    assert exp > before
    # within the documented 8h window (allow small clock slack)
    assert exp <= before + timedelta(hours=security.JWT_EXPIRY_HOURS, seconds=5)


def test_decode_rejects_wrong_secret() -> None:
    bad = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "a-totally-different-secret-key-32chars!!",
        algorithm=security.JWT_ALGORITHM,
    )
    with pytest.raises(JWTError):
        security.decode_access_token(bad)


def test_decode_rejects_expired_token() -> None:
    secret = get_settings().secret_key
    expired = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) - timedelta(hours=1)},
        secret,
        algorithm=security.JWT_ALGORITHM,
    )
    with pytest.raises(JWTError):
        security.decode_access_token(expired)


def test_decode_rejects_garbage() -> None:
    with pytest.raises(JWTError):
        security.decode_access_token("not.a.jwt")


def test_decode_rejects_token_signed_with_different_algorithm() -> None:
    # 'none' alg / mismatched algorithm must not be accepted.
    secret = get_settings().secret_key
    token = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(hours=1)},
        secret,
        algorithm="HS512",
    )
    with pytest.raises(JWTError):
        security.decode_access_token(token)


# ── bcrypt's 72-byte limit ───────────────────────────────────────────────────


def test_verify_treats_overlong_password_as_wrong() -> None:
    """bcrypt 5 raises on >72 bytes; a login attempt must never turn that into a 500."""
    from submissions_checker.core.security import hash_password, verify_password

    hashed = hash_password("short")
    assert verify_password("a" * 80, hashed) is False


def test_hash_refuses_overlong_password() -> None:
    from submissions_checker.core.security import hash_password

    with pytest.raises(ValueError):
        hash_password("a" * 73)


def test_password_too_long_counts_bytes_not_characters() -> None:
    from submissions_checker.core.security import password_too_long

    assert password_too_long("a" * 72) is False
    assert password_too_long("a" * 73) is True
    # 3 bytes per character in UTF-8.
    assert password_too_long("я" * 37) is True  # 2 bytes each: 74


def test_token_carries_issue_time() -> None:
    """`iat` is what lets a password change refuse older sessions."""
    import time

    from submissions_checker.core.security import create_access_token, decode_access_token

    before = int(time.time())
    payload = decode_access_token(create_access_token(1, "u", "TEACHER"))
    assert isinstance(payload["iat"], int)
    assert before <= payload["iat"] <= int(time.time())
