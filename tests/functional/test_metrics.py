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


async def test_metrics_endpoint_does_not_count_itself(client: AsyncClient) -> None:
    from submissions_checker.core import metrics

    own = metrics.http_requests_total.labels(route="/metrics", method="GET", status_class="2xx")
    before = own._value.get()
    await client.get("/metrics")
    await client.get("/metrics")
    assert own._value.get() == before
    assert metrics.http_requests_in_progress._value.get() == 0


async def test_login_page_is_counted_by_template(client: AsyncClient) -> None:
    await client.get("/auth/login")
    body = (await client.get("/metrics")).text
    assert 'http_requests_total{method="GET",route="/auth/login",status_class="2xx"}' in body
