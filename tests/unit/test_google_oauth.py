from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from submissions_checker.core.config import Settings
from submissions_checker.services.google.crypto import decrypt_token, encrypt_token
from submissions_checker.services.google.oauth import (
    GoogleAuthError,
    authorization_url,
    exchange_code,
    new_pkce,
    refresh_access_token,
    revoke,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        secret_key="x" * 32,
        database_url="postgresql+asyncpg://u:p@h/db",
        google_client_id="cid",
        google_client_secret="sec",
        google_token_encryption_key=Fernet.generate_key().decode(),
        app_base_url="https://chk.example/",
    )


def test_encrypt_roundtrip():
    s = _settings()
    ct = encrypt_token(s, "rt")
    assert ct != "rt"
    assert decrypt_token(s, ct) == "rt"


def test_decrypt_with_wrong_key_raises():
    ct = encrypt_token(_settings(), "rt")
    with pytest.raises(GoogleAuthError):
        decrypt_token(_settings(), ct)


def test_pkce_challenge_is_s256_of_verifier():
    import base64
    import hashlib

    p = new_pkce()
    assert 43 <= len(p.verifier) <= 96
    want = base64.urlsafe_b64encode(hashlib.sha256(p.verifier.encode()).digest()).rstrip(b"=")
    assert p.challenge == want.decode()


def test_auth_url_has_pkce_offline_and_redirect():
    s = _settings()
    u = authorization_url(s, "st", new_pkce())
    q = parse_qs(urlparse(u).query)
    assert q["redirect_uri"] == ["https://chk.example/teacher/google/callback"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]
    assert "drive.readonly" in q["scope"][0]


async def test_exchange_returns_email_and_refresh():
    def h(req):
        if req.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "scope": "https://www.googleapis.com/auth/classroom.student-submissions.students.readonly",
                },
            )
        assert req.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json={"email": "t@edu.kpi.ua"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        assert await exchange_code(_settings(), "code", "ver", http) == ("t@edu.kpi.ua", "rt")


async def test_exchange_without_refresh_token_fails():
    h = lambda req: httpx.Response(200, json={"access_token": "at"})  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        with pytest.raises(GoogleAuthError):
            await exchange_code(_settings(), "code", "ver", http)


async def test_refresh_invalid_grant_flagged():
    h = lambda req: httpx.Response(400, json={"error": "invalid_grant"})  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        with pytest.raises(GoogleAuthError) as ei:
            await refresh_access_token(_settings(), "rt", http)
    assert ei.value.invalid_grant


async def test_refresh_other_error_not_invalid_grant():
    h = lambda req: httpx.Response(500, text="boom")  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        with pytest.raises(GoogleAuthError) as ei:
            await refresh_access_token(_settings(), "rt", http)
    assert not ei.value.invalid_grant


async def test_revoke_swallows_errors():
    def h(req):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        await revoke("rt", http)
