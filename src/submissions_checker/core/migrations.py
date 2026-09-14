"""Database migration runner — delegates to Alembic under a PostgreSQL advisory lock.

Migrations run during application startup. Under a rolling deploy two replicas can boot
at the same time, and two concurrent ``alembic upgrade head`` runs race on DDL. The
advisory lock serializes them: the first replica applies the migrations, the second
waits and then finds nothing to do.

The lock is session-scoped, so PostgreSQL releases it automatically if the holding
replica is killed mid-migration — a crashed deploy cannot wedge the next boot.
"""

import logging
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from submissions_checker.core.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


def _alembic_config_path() -> Path:
    """Locate alembic.ini.

    The development image installs the package editable from ``/app/src``, so a
    package-relative path lands on ``/app``. The production image installs it into
    site-packages while ``alembic.ini`` and the migration scripts stay in the working
    directory — there, the package-relative path resolves to the interpreter's lib
    directory and Alembic fails with "No 'script_location' key found in configuration".
    Prefer the working directory, which is correct in both.
    """
    cwd_ini = Path.cwd() / "alembic.ini"
    if cwd_ini.is_file():
        return cwd_ini
    return _PROJECT_ROOT / "alembic.ini"


# Distinct from OUTBOX_PROCESSOR_LOCK_ID (7919) and TEACHER_DIGEST_LOCK_ID (7927);
# sharing an id with either would deadlock startup against a running scheduler.
MIGRATION_LOCK_ID = 7933


def _migration_engine() -> AsyncEngine:
    """A throwaway engine for the lock connection.

    Migrations run before ``init_db()``, so the application pool does not exist yet.
    ``NullPool`` keeps this from holding a connection open past the migration.
    """
    settings = get_settings()
    return create_async_engine(str(settings.database_url), poolclass=NullPool)


async def run_migrations() -> None:
    """Run all pending Alembic migrations (upgrade to head), serialized across replicas."""
    import asyncio
    import os

    logger.info("starting_alembic_migration_runner")
    cfg = Config(_alembic_config_path())
    # Tell env.py to skip fileConfig() — it deadlocks logging's RLock
    # when structlog is active in the main asyncio thread.
    os.environ["_ALEMBIC_SKIP_FILECONFIG"] = "1"

    engine = _migration_engine()
    try:
        async with engine.connect() as conn:
            # Blocking acquisition, not pg_try_*: a replica that loses the race must wait
            # and then proceed, never skip migrating and serve against an old schema.
            logger.info("awaiting_migration_lock")
            await conn.execute(
                text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
            )
            logger.info("migration_lock_acquired")
            try:
                await asyncio.to_thread(command.upgrade, cfg, "head")
            finally:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID}
                )
                logger.info("migration_lock_released")
    finally:
        os.environ.pop("_ALEMBIC_SKIP_FILECONFIG", None)
        await engine.dispose()

    logger.info("migrations_completed")
