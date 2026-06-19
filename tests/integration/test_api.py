"""API integration tests."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
async def test_readiness_check(db_session: AsyncSession) -> None:
    """Test readiness check with database connectivity."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_root_endpoint() -> None:
    """Test root endpoint returns API information."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "Submissions Checker"
    assert "version" in data
    assert "status" in data


@pytest.mark.asyncio
async def test_user_endpoint_skeleton() -> None:
    """Test user creation endpoint (skeleton)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/users")

    # Should return 200 (skeleton returns not implemented)
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
