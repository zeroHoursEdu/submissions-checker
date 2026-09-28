"""Per-request log context and one access line per request.

Binds a request id into structlog's contextvars, so every line logged while serving the
request carries it; echoes it as ``X-Request-ID`` so a student's screenshot leads straight to
their request; and emits ``http_request`` when the response is done.

The user is only known once the auth dependency ran, which may be in a copied context (sync
dependencies run in a thread), so :func:`bind_user` also writes into a per-request holder
that this middleware owns — a plain ``bind_contextvars`` there might never reach it.

Never logged: query string (reset tokens live there), body, cookies, headers.
"""

from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from submissions_checker.core.logging import get_logger
from submissions_checker.core.metrics_middleware import route_template_for

logger = get_logger(__name__)

_SKIP_PREFIXES = ("/health", "/metrics", "/static")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
_user_holder: ContextVar[dict[str, Any] | None] = ContextVar("request_log_user", default=None)


def bind_user(user_id: int, role: str) -> None:
    """Attach the authenticated user to every later line of this request."""
    structlog.contextvars.bind_contextvars(user_id=user_id, role=role)
    holder = _user_holder.get()
    if holder is not None:
        holder["user_id"] = user_id
        holder["role"] = role


def level_for(status: int) -> str:
    if status >= 500:
        return "error"
    # 401 is every expired session, 404 every scanner: normal traffic, not warnings.
    if status >= 400 and status not in (401, 404):
        return "warning"
    return "info"


def _request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            if _REQUEST_ID_RE.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex[:16]


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        structlog.contextvars.clear_contextvars()
        request_id = _request_id(scope)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        holder: dict[str, Any] = {}
        token = _user_holder.set(holder)
        # An exception that escapes the app never sends a response start; the client sees
        # a 500, so that is what it is logged as.
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _user_holder.reset(token)
            if not scope["path"].startswith(_SKIP_PREFIXES):
                status = status_holder["status"]
                getattr(logger, level_for(status))(
                    "http_request",
                    method=scope.get("method"),
                    route=route_template_for(scope),
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    **holder,
                )
            structlog.contextvars.clear_contextvars()
