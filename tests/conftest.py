"""Pytest configuration and shared fixtures.

``testcontainers`` (and the ``redis`` client it pulls in) are imported lazily
inside the fixtures that need them, not at module top level. That keeps this
top-level conftest importable for test suites that don't use containers — most
importantly the e2e/BDD suite (which talks to a live Dockerised stack via its
own config) and any unit-only run — without requiring the dev/redis extras to
be installed just to collect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

# Importing the models package ensures every table (not just the subset wired
# into db.base) is registered on Base.metadata before create_all runs.
import submissions_checker.db.models  # noqa: F401
from submissions_checker.core.config import Settings
from submissions_checker.db.base import Base

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer


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
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def redis_container() -> Generator[RedisContainer, None, None]:
    """
    Create a Redis test container.

    Uses testcontainers to spin up a Redis instance for integration tests.
    """
    from testcontainers.redis import RedisContainer

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


@pytest.fixture
async def test_engine(test_settings: Settings) -> AsyncGenerator[AsyncEngine, None]:
    """
    Create a test database engine bound to the running test's event loop.

    This is intentionally function-scoped. A session-scoped engine is created on
    a different event loop than the per-test loop, and reusing its asyncpg
    connection across loops raises "another operation is in progress". A fresh
    engine + fresh schema per test keeps each test isolated and loop-safe.

    Args:
        test_settings: Test settings with database URL

    Yields:
        Async database engine
    """
    engine = create_async_engine(str(test_settings.database_url))

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
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        yield session
        await session.rollback()
