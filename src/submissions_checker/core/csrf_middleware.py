"""Refuse state-changing requests that a browser marks as cross-site.

Session cookies are SameSite=Strict, which already blocks CSRF in every browser
that honours it. This is the second layer OWASP recommends: Fetch Metadata
(``Sec-Fetch-Site``) when present, ``Origin`` vs ``Host`` otherwise. Requests
that carry neither header (curl, tests, old clients) are allowed - a browser
always sends at least one of them on a cross-site POST.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return str(v.decode("latin-1"))
    return None


def is_cross_site(scope: Scope) -> bool:
    site = _header(scope, b"sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = _header(scope, b"origin")
    if origin is None:
        return False
    if origin == "null":
        return True
    host = _header(scope, b"host") or ""
    return urlsplit(origin).netloc.lower() != host.lower()


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "GET") in _UNSAFE
            and is_cross_site(scope)
        ):
            response = JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
