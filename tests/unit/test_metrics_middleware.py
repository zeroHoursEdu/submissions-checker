"""Request counting by route template, never by raw path."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from submissions_checker.core import metrics
from submissions_checker.core.metrics_middleware import PrometheusMiddleware


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/metrics")
    async def metrics_route() -> str:
        return "ok"

    app.add_middleware(PrometheusMiddleware)
    return app


def _count(route: str, method: str, status_class: str) -> float:
    return metrics.http_requests_total.labels(
        route=route, method=method, status_class=status_class
    )._value.get()


@pytest.mark.asyncio
async def test_counts_by_route_template_not_path() -> None:
    before = _count("/items/{item_id}", "GET", "2xx")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/items/1")
        await c.get("/items/2")
    assert _count("/items/{item_id}", "GET", "2xx") == before + 2


@pytest.mark.asyncio
async def test_unknown_paths_collapse_to_unmatched() -> None:
    before = _count("unmatched", "GET", "4xx")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/wp-admin.php")
        await c.get("/.env")
    assert _count("unmatched", "GET", "4xx") == before + 2


@pytest.mark.asyncio
async def test_exceptions_count_as_5xx_and_in_progress_returns_to_zero() -> None:
    before = _count("/boom", "GET", "5xx")
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/boom")
    assert _count("/boom", "GET", "5xx") == before + 1
    assert metrics.http_requests_in_progress._value.get() == 0


@pytest.mark.asyncio
async def test_metrics_and_static_are_not_counted() -> None:
    metrics_before = _count("/metrics", "GET", "2xx")
    unmatched_before = _count("unmatched", "GET", "4xx")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/metrics")
        await c.get("/static/app.css")  # 404 here, but skipped by prefix before routing
    assert _count("/metrics", "GET", "2xx") == metrics_before
    assert _count("unmatched", "GET", "4xx") == unmatched_before


@pytest.mark.asyncio
async def test_latency_is_observed_once_per_request() -> None:
    before = metrics.http_request_duration_seconds._sum.get()
    count_before = sum(b.get() for b in metrics.http_request_duration_seconds._buckets)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        await c.get("/items/1")
    assert metrics.http_request_duration_seconds._sum.get() >= before
    assert sum(b.get() for b in metrics.http_request_duration_seconds._buckets) == count_before + 1


@pytest.mark.asyncio
async def test_routes_inside_included_routers_resolve_to_their_template() -> None:
    """The real app mounts every route through APIRouter.include_router; those must count
    by template too, not collapse to 'unmatched'."""
    from fastapi import APIRouter

    app = FastAPI()
    router = APIRouter(prefix="/portal")

    @router.get("/quiz/{attempt_id}")
    async def quiz(attempt_id: int) -> dict[str, int]:
        return {"id": attempt_id}

    app.include_router(router)
    app.add_middleware(PrometheusMiddleware)

    before = _count("/portal/quiz/{attempt_id}", "GET", "2xx")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/portal/quiz/7")
    assert _count("/portal/quiz/{attempt_id}", "GET", "2xx") == before + 1
