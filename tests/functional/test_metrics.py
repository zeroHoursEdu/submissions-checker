"""GET /metrics through the real app."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_metrics_is_unauthenticated_text_exposition(client: AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in resp.text
    assert "students_total" in resp.text
