"""Every response carries the browser hardening headers, not only the ones Caddy adds."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from submissions_checker.core.security_headers import SecurityHeadersMiddleware


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/x")
    async def x() -> dict[str, int]:
        return {"ok": 1}

    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.mark.asyncio
async def test_headers_present_on_every_response() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/x")
    h = r.headers
    csp = h["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp
    assert "default-src 'self'" in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "SAMEORIGIN"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    pp = h["permissions-policy"]
    assert "camera=(self)" in pp and "geolocation=(self)" in pp and "microphone=()" in pp


@pytest.mark.asyncio
async def test_headers_present_on_404() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/nope")
    assert r.status_code == 404
    assert "content-security-policy" in r.headers
