"""Request id, user binding and the access line."""

from __future__ import annotations

import re

import structlog
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from submissions_checker.core.logging import get_logger
from submissions_checker.core.request_logging import RequestLoggingMiddleware, bind_user, level_for


def _app() -> FastAPI:
    app = FastAPI()
    inner = get_logger("t.inner")

    async def user() -> None:
        bind_user(42, "STUDENT")

    @app.get("/items/{item_id}", dependencies=[Depends(user)])
    async def item(item_id: int) -> dict[str, int]:
        inner.info("inside_handler")
        return {"id": item_id}

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403)

    @app.get("/boom", dependencies=[Depends(user)])
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(RequestLoggingMiddleware)
    return app


def _client() -> AsyncClient:
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://t")


def _entries(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e["event"] == event]


async def test_generates_request_id_and_echoes_it() -> None:
    async with _client() as c:
        resp = await c.get("/items/1")
    assert re.fullmatch(r"[0-9a-f]{16}", resp.headers["x-request-id"])


async def test_keeps_a_well_formed_incoming_request_id_and_replaces_a_bad_one() -> None:
    async with _client() as c:
        good = await c.get("/items/1", headers={"X-Request-ID": "abc-12345678"})
        bad = await c.get("/items/1", headers={"X-Request-ID": "bad id <script>"})
    assert good.headers["x-request-id"] == "abc-12345678"
    assert re.fullmatch(r"[0-9a-f]{16}", bad.headers["x-request-id"])


async def test_every_line_in_the_request_carries_request_and_user_ids() -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        async with _client() as c:
            resp = await c.get("/items/5?token=secret-reset-token")
    rid = resp.headers["x-request-id"]
    inner = _entries(logs, "inside_handler")[0]
    assert inner["request_id"] == rid and inner["user_id"] == 42
    access = _entries(logs, "http_request")[0]
    assert access["request_id"] == rid
    assert access["user_id"] == 42 and access["role"] == "STUDENT"
    assert access["route"] == "/items/{item_id}"
    assert access["method"] == "GET" and access["status"] == 200
    assert access["log_level"] == "info"
    assert isinstance(access["duration_ms"], float)
    assert "secret-reset-token" not in repr(logs)


async def test_levels_by_status() -> None:
    with capture_logs() as logs:
        async with _client() as c:
            await c.get("/forbidden")
            await c.get("/boom")
            await c.get("/nope")
    levels = {(e["status"], e["log_level"]) for e in _entries(logs, "http_request")}
    assert levels == {(403, "warning"), (500, "error"), (404, "info")}


async def test_unhandled_exception_is_logged_with_the_request_context() -> None:
    """uvicorn logs the traceback after the middleware returned and the context is gone,
    so the middleware logs it itself while request_id/user_id are still bound."""
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        async with _client() as c:
            resp = await c.get("/boom")
    assert resp.status_code == 500
    failed = _entries(logs, "http_request_failed")
    assert len(failed) == 1
    line = failed[0]
    assert line["request_id"] and line["user_id"] == 42 and line["role"] == "STUDENT"
    assert line["exc_info"] is True
    assert line["log_level"] == "error"
    assert line["route"] == "/boom" and line["method"] == "GET"
    access = _entries(logs, "http_request")[0]
    assert access["status"] == 500 and access["request_id"] == line["request_id"]


async def test_health_is_not_logged() -> None:
    with capture_logs() as logs:
        async with _client() as c:
            await c.get("/health")
    assert not _entries(logs, "http_request")


async def test_context_does_not_leak_between_requests() -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        async with _client() as c:
            await c.get("/items/1")
            await c.get("/forbidden")
    second = _entries(logs, "http_request")[1]
    assert "user_id" not in second


def test_level_for() -> None:
    assert [level_for(s) for s in (200, 302, 401, 403, 404, 422, 500)] == [
        "info",
        "info",
        "info",
        "warning",
        "info",
        "warning",
        "error",
    ]
