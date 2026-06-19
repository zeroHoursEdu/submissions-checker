"""Pytest configuration and shared fixtures."""

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from submissions_checker.core.config import Settings
from submissions_checker.db.base import Base


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """
    Create an event loop for the test session.

    This fixture provides a single event loop for all async tests.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """
    Create a PostgreSQL test container.

    Uses testcontainers to spin up a PostgreSQL instance for integration tests.
    """
    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def redis_container() -> Generator[RedisContainer, None, None]:
    """
    Create a Redis test container.

    Uses testcontainers to spin up a Redis instance for integration tests.
    """
    with RedisContainer("redis:7-alpine") as redis:
        yield redis


@pytest.fixture(scope="session")
def test_settings(
    postgres_container: PostgresContainer,
    redis_container: RedisContainer,
) -> Settings:
    """
    Create test settings with container connection details.

    Args:
        postgres_container: PostgreSQL test container
        redis_container: Redis test container

    Returns:
        Settings configured for testing
    """
    return Settings(
        environment="test",
        database_url=postgres_container.get_connection_url(driver="asyncpg"),
        redis_url=f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0",
        secret_key="test-secret-key-minimum-32-chars-long",
        log_level="DEBUG",
    )


@pytest.fixture(scope="session")
async def test_engine(test_settings: Settings) -> AsyncGenerator[AsyncEngine, None]:
    """
    Create a test database engine.

    Args:
        test_settings: Test settings with database URL

    Yields:
        Async database engine
    """
    engine = create_async_engine(str(test_settings.database_url), echo=True)

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """
    Create a test database session.

    This fixture provides a database session for each test and automatically
    rolls back changes after the test completes.

    Args:
        test_engine: Test database engine

    Yields:
        Database session
    """
    async with AsyncSession(test_engine) as session:
        yield session
        await session.rollback()


@pytest.fixture
def mock_github_webhook_payload() -> dict[str, Any]:
    """
    Mock GitHub webhook payload for testing.

    Returns:
        Sample pull request webhook payload
    """
    return {
        "action": "opened",
        "number": 123,
        "pull_request": {
            "number": 123,
            "title": "Test PR",
            "head": {"sha": "abc123", "ref": "feature-branch"},
            "base": {"ref": "main"},
        },
        "repository": {
            "full_name": "test-org/test-repo",
            "clone_url": "https://github.com/test-org/test-repo.git",
        },
        "sender": {"login": "test-user"},
    }
