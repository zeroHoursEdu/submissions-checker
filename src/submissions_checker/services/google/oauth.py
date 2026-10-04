"""Google OAuth 2.0 (authorization code + PKCE) over plain httpx.

No google client libraries: they compare granted scopes literally and crash on
Google's alias scopes. Granted scopes are therefore never inspected here.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from submissions_checker.core.config import Settings

_API = "https://www.googleapis.com/auth/"
SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    _API + "classroom.courses.readonly",
    _API + "classroom.coursework.students.readonly",
    _API + "classroom.rosters.readonly",
    _API + "classroom.profile.emails",
    _API + "drive.readonly",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleAuthError(RuntimeError):
    """OAuth failure. `invalid_grant` means the refresh token is dead."""

    def __init__(self, message: str, *, invalid_grant: bool = False) -> None:
        super().__init__(message)
        self.invalid_grant = invalid_grant


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


def new_pkce() -> PkcePair:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(verifier=verifier, challenge=challenge)


def redirect_uri(settings: Settings) -> str:
    return f"{settings.app_base_url.rstrip('/')}/teacher/google/callback"


def authorization_url(settings: Settings, state: str, pkce: PkcePair) -> str:
    params = {
        "client_id": settings.google_client_id or "",
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _error_from(resp: httpx.Response, what: str) -> GoogleAuthError:
    try:
        error = str(resp.json().get("error", ""))
    except (ValueError, AttributeError):
        error = ""
    return GoogleAuthError(
        f"{what} failed: HTTP {resp.status_code} {error}".strip(),
        invalid_grant=error == "invalid_grant",
    )


async def exchange_code(
    settings: Settings, code: str, verifier: str, http: httpx.AsyncClient
) -> tuple[str, str]:
    """Trade an auth code for (email, refresh_token)."""
    resp = await http.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id or "",
            "client_secret": settings.google_client_secret or "",
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(settings),
        },
    )
    if resp.status_code != 200:
        raise _error_from(resp, "code exchange")
    body = resp.json()
    refresh_token = body.get("refresh_token")
    access_token = body.get("access_token")
    if not refresh_token or not access_token:
        raise GoogleAuthError("code exchange returned no refresh_token")
    info = await http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    if info.status_code != 200:
        raise _error_from(info, "userinfo")
    email = info.json().get("email")
    if not email:
        raise GoogleAuthError("userinfo returned no email")
    return str(email), str(refresh_token)


async def refresh_access_token(
    settings: Settings, refresh_token: str, http: httpx.AsyncClient
) -> str:
    resp = await http.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id or "",
            "client_secret": settings.google_client_secret or "",
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code != 200:
        raise _error_from(resp, "token refresh")
    token = resp.json().get("access_token")
    if not token:
        raise GoogleAuthError("token refresh returned no access_token")
    return str(token)


async def revoke(refresh_token: str, http: httpx.AsyncClient) -> None:
    """Best effort: the local row is deleted regardless."""
    try:
        await http.post(REVOKE_URL, data={"token": refresh_token})
    except httpx.HTTPError:
        pass
