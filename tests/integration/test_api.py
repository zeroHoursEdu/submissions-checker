"""API integration tests."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from submissions_checker.core.database import get_db
from submissions_checker.main import app


@pytest.mark.asyncio
async def test_health_check() -> None:
    """Test basic health check endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_readiness_check(test_engine: AsyncEngine) -> None:
    """Test readiness check with database connectivity.

    Under ASGITransport the app's lifespan does not run, so the global engine in
    ``core.database`` is never initialized. Override ``get_db`` to yield a session
    bound to the test engine so the readiness DB probe runs against the container.
    """

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_root_endpoint() -> None:
    """Test root endpoint redirects to the login page."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login"
