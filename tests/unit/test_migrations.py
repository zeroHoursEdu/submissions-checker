"""Unit tests for the Alembic migration runner wrapper.

Alembic's ``command.upgrade`` and ``Config`` are patched so no real database or
migration script runs. The tests assert the config is built from the project's
``alembic.ini``, ``upgrade head`` is invoked off-thread, the deadlock-avoidance
env flag is set during the run and cleaned up afterwards (even on failure), and
that runner errors propagate.
"""

from __future__ import annotations

import os

import pytest

from submissions_checker.core import migrations as migrations_module


class _RecordingConn:
    """Captures the advisory-lock statements run around the Alembic upgrade."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def execute(self, statement, params=None):  # noqa: ANN001
        self.log.append(f"{str(statement).strip()} {params or ''}".strip())
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingEngine:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.disposed = False

    def connect(self):
        return _RecordingConn(self.log)

    async def dispose(self):
        self.disposed = True


def _patch_engine(monkeypatch, log: list[str]) -> _RecordingEngine:
    engine = _RecordingEngine(log)
    monkeypatch.setattr(migrations_module, "_migration_engine", lambda: engine)
    return engine


async def test_run_migrations_invokes_upgrade_head(monkeypatch) -> None:
    captured = {}

    def fake_upgrade(cfg, rev):
        captured["cfg"] = cfg
        captured["rev"] = rev
        # While running, the deadlock-avoidance flag must be set.
        captured["flag_during"] = os.environ.get("_ALEMBIC_SKIP_FILECONFIG")

    fake_config = object()

    def fake_config_ctor(path):
        captured["config_path"] = path
        return fake_config

    _patch_engine(monkeypatch, [])
    monkeypatch.setattr(
        migrations_module.command, "upgrade", fake_upgrade
    )
    monkeypatch.setattr(migrations_module, "Config", fake_config_ctor)
    monkeypatch.delenv("_ALEMBIC_SKIP_FILECONFIG", raising=False)

    await migrations_module.run_migrations()

    assert captured["rev"] == "head"
    assert captured["cfg"] is fake_config
    assert captured["flag_during"] == "1"
    # Config built from the project's alembic.ini.
    assert str(captured["config_path"]).endswith("alembic.ini")
    # Flag cleaned up after a successful run.
    assert "_ALEMBIC_SKIP_FILECONFIG" not in os.environ


async def test_run_migrations_cleans_up_flag_on_error(monkeypatch) -> None:
    def boom(cfg, rev):
        raise RuntimeError("migration exploded")

    _patch_engine(monkeypatch, [])
    monkeypatch.setattr(migrations_module.command, "upgrade", boom)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())
    monkeypatch.delenv("_ALEMBIC_SKIP_FILECONFIG", raising=False)

    with pytest.raises(RuntimeError, match="migration exploded"):
        await migrations_module.run_migrations()

    # The finally block must remove the env flag even on failure.
    assert "_ALEMBIC_SKIP_FILECONFIG" not in os.environ


async def test_run_migrations_holds_advisory_lock_around_upgrade(monkeypatch) -> None:
    """The upgrade must happen strictly between lock acquisition and release."""
    log: list[str] = []
    engine = _patch_engine(monkeypatch, log)

    def fake_upgrade(cfg, rev):
        log.append("UPGRADE")

    monkeypatch.setattr(migrations_module.command, "upgrade", fake_upgrade)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())

    await migrations_module.run_migrations()

    assert len(log) == 3, log
    assert "pg_advisory_lock" in log[0]
    assert log[1] == "UPGRADE"
    assert "pg_advisory_unlock" in log[2]
    assert engine.disposed is True


async def test_migration_lock_id_is_distinct_from_other_advisory_locks() -> None:
    """Sharing a lock id with the schedulers would deadlock startup against them."""
    from submissions_checker.workers.scheduled.outbox_processor import OUTBOX_PROCESSOR_LOCK_ID
    from submissions_checker.workers.scheduled.teacher_digest_processor import (
        TEACHER_DIGEST_LOCK_ID,
    )

    assert migrations_module.MIGRATION_LOCK_ID not in {
        OUTBOX_PROCESSOR_LOCK_ID,
        TEACHER_DIGEST_LOCK_ID,
    }


async def test_run_migrations_releases_lock_when_upgrade_raises(monkeypatch) -> None:
    """A failed migration must not leave the lock held — the next boot has to retry."""
    log: list[str] = []
    engine = _patch_engine(monkeypatch, log)

    def boom(cfg, rev):
        log.append("UPGRADE")
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(migrations_module.command, "upgrade", boom)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())

    with pytest.raises(RuntimeError, match="migration exploded"):
        await migrations_module.run_migrations()

    assert "pg_advisory_lock" in log[0]
    assert "pg_advisory_unlock" in log[-1]
    assert engine.disposed is True
    assert "_ALEMBIC_SKIP_FILECONFIG" not in os.environ


def test_alembic_config_is_found_from_the_working_directory(tmp_path, monkeypatch) -> None:
    """The production image installs the package into site-packages, not /app/src.

    A package-relative path then resolves to the interpreter's lib directory and Alembic
    fails with "No 'script_location' key found in configuration". The working directory
    is correct for both the editable development install and the production image.
    """
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    monkeypatch.chdir(tmp_path)

    assert migrations_module._alembic_config_path() == tmp_path / "alembic.ini"


def test_alembic_config_falls_back_to_the_package_root(tmp_path, monkeypatch) -> None:
    """With no alembic.ini in the working directory, fall back to the repo layout."""
    monkeypatch.chdir(tmp_path)  # contains no alembic.ini

    assert migrations_module._alembic_config_path() == (
        migrations_module._PROJECT_ROOT / "alembic.ini"
    )
