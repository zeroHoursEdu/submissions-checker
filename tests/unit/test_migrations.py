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

    monkeypatch.setattr(migrations_module.command, "upgrade", boom)
    monkeypatch.setattr(migrations_module, "Config", lambda path: object())
    monkeypatch.delenv("_ALEMBIC_SKIP_FILECONFIG", raising=False)

    with pytest.raises(RuntimeError, match="migration exploded"):
        await migrations_module.run_migrations()

    # The finally block must remove the env flag even on failure.
    assert "_ALEMBIC_SKIP_FILECONFIG" not in os.environ
