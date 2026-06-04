"""Database migration runner — delegates to Alembic."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


async def run_migrations() -> None:
    """Run all pending Alembic migrations (upgrade to head)."""
    import asyncio
    import os

    logger.info("starting_alembic_migration_runner")
    cfg = Config(_PROJECT_ROOT / "alembic.ini")
    # Tell env.py to skip fileConfig() — it deadlocks logging's RLock
    # when structlog is active in the main asyncio thread.
    os.environ["_ALEMBIC_SKIP_FILECONFIG"] = "1"
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ.pop("_ALEMBIC_SKIP_FILECONFIG", None)
    logger.info("migrations_completed")
