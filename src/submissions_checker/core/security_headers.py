"""Browser hardening headers on every response.

Caddy sets a similar set in production, but the application must not depend on the
proxy in front of it: the development and e2e stacks talk to uvicorn directly, and a
future deployment behind a different proxy would otherwise ship bare.

The CSP is deliberately modest. Templates carry inline scripts and styles and Tailwind
is loaded from its CDN, so ``'unsafe-inline'`` stays until the assets are vendored; the
policy still forbids framing by other origins (clickjacking), ``<base>`` hijacking,
posting forms to other origins, and plugin objects. Camera and geolocation are allowed
for the page itself because the proctored quiz uses both.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self' https://cdn.tailwindcss.com",
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "font-src 'self' data:",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ]
)

PERMISSIONS_POLICY = "camera=(self), geolocation=(self), microphone=(), payment=(), usb=()"

_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
