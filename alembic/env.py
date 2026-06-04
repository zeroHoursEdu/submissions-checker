"""Alembic environment — sync psycopg2 migration runner."""

from logging.config import fileConfig

from sqlalchemy import create_engine

# Import all models so Base.metadata is fully populated
import submissions_checker.db.models  # noqa: F401
from alembic import context
from submissions_checker.core.config import get_settings
from submissions_checker.db.models.base import Base

config = context.config

# Skip fileConfig when called from within the app — the app already configures
# logging, and calling fileConfig from asyncio.to_thread deadlocks on
# logging's internal RLock when structlog is active in the main thread.
import os as _os
if config.config_file_name is not None and not _os.environ.get("_ALEMBIC_SKIP_FILECONFIG"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_sync_url() -> str:
    url = str(get_settings().database_url)
    # Replace async driver with psycopg2 for the sync migration runner
    return url.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    context.configure(
        url=get_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_sync_url())
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
