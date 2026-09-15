"""ASGI middleware that counts requests by route template.

Starlette does not put the matched route on the scope, but it does set
``scope["endpoint"]``; the template is looked up from the app's flattened route table, so
``/portal/quiz/17`` counts as ``/portal/quiz/{attempt_id}``. Anything that matches no route
counts as ``unmatched`` — path scanners must not be able to mint series.
"""

from __future__ import annotations

import time

from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from submissions_checker.core import metrics

_SKIP_PREFIXES = ("/metrics", "/static")
UNMATCHED = "unmatched"


def route_template_for(scope: Scope) -> str:
    endpoint = scope.get("endpoint")
    app = scope.get("app")
    if endpoint is None or app is None:
        return UNMATCHED
    for route in app.routes:
        # APIRoute subclasses Route; Mount (static files) does not and is skipped.
        if isinstance(route, Route) and route.endpoint is endpoint:
            return str(route.path)
    return UNMATCHED


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


class PrometheusMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        # An exception that escapes the app never sends a response start; it surfaces
        # as a 500 to the client, so that is what it counts as.
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        metrics.http_requests_in_progress.inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.http_requests_in_progress.dec()
            metrics.http_request_duration_seconds.observe(time.perf_counter() - started)
            metrics.http_requests_total.labels(
                route=route_template_for(scope),
                method=scope.get("method", "GET"),
                status_class=_status_class(status_holder["status"]),
            ).inc()
