"""Integration test: concurrent migration runs serialize on a real PostgreSQL advisory lock.

During a rolling deploy two replicas can boot at the same time and both call
``run_migrations()``. Without coordination they race on Alembic DDL. These tests use a
real database so the locking semantics — blocking acquisition, and release when the
holding session dies — are exercised for real rather than against a fake.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from submissions_checker.core import migrations as migrations_module
from submissions_checker.core.config import Settings


@pytest.fixture
def patched_settings(monkeypatch, test_settings: Settings) -> Settings:
    """Point the migration runner at the test container instead of the real environment."""
    monkeypatch.setattr(migrations_module, "get_settings", lambda: test_settings)
    return test_settings


async def test_concurrent_migrations_do_not_overlap(monkeypatch, patched_settings) -> None:
    """Two simultaneous runs must not have their upgrades interleaved."""
    events: list[str] = []

    def slow_upgrade(cfg, rev):
        events.append("enter")
        # Long enough that an unserialized second runner would demonstrably overlap.
        import time

        time.sleep(0.3)
        events.append("exit")

    monkeypatch.setattr(migrations_module.command, "upgrade", slow_upgrade)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())

    await asyncio.gather(
        migrations_module.run_migrations(),
        migrations_module.run_migrations(),
    )

    # Strict alternation proves the second runner waited for the first to finish.
    assert events == ["enter", "exit", "enter", "exit"], events


async def test_lock_is_released_after_a_successful_run(monkeypatch, patched_settings) -> None:
    """A later boot must be able to take the lock again."""
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, rev: None)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())

    await migrations_module.run_migrations()

    engine = create_async_engine(str(patched_settings.database_url))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": migrations_module.MIGRATION_LOCK_ID},
            )
            assert result.scalar() is True, "migration lock was left held"
            await conn.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": migrations_module.MIGRATION_LOCK_ID},
            )
    finally:
        await engine.dispose()


async def test_lock_is_released_when_the_migration_fails(monkeypatch, patched_settings) -> None:
    """A crashed migration must not wedge every subsequent boot."""

    def boom(cfg, rev):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(migrations_module.command, "upgrade", boom)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())

    with pytest.raises(RuntimeError, match="migration exploded"):
        await migrations_module.run_migrations()

    # The next run acquires the lock without blocking and completes.
    monkeypatch.setattr(migrations_module.command, "upgrade", lambda cfg, rev: None)
    await asyncio.wait_for(migrations_module.run_migrations(), timeout=10)
