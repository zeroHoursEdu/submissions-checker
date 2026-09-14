"""Unit tests for the APScheduler-backed background scheduler.

``AsyncIOScheduler`` is replaced with a mock so nothing real is started. The
tests assert job registration (ids + interval triggers) and the
init/start/shutdown lifecycle, including the idempotency guards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from submissions_checker.core import scheduler as scheduler_module


@pytest.fixture(autouse=True)
def _reset_global_scheduler():
    """Ensure each test starts and ends with no global scheduler instance."""
    scheduler_module._scheduler = None
    yield
    scheduler_module._scheduler = None


def _fake_scheduler() -> MagicMock:
    sched = MagicMock()
    sched.running = False
    return sched


def _install_fake_scheduler(monkeypatch, sched: MagicMock) -> None:
    monkeypatch.setattr(scheduler_module, "AsyncIOScheduler", lambda *a, **k: sched)


def test_get_scheduler_before_init_raises() -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        scheduler_module.get_scheduler()


def test_init_scheduler_registers_jobs_with_intervals(monkeypatch) -> None:
    sched = _fake_scheduler()
    _install_fake_scheduler(monkeypatch, sched)

    fake_settings = MagicMock(teacher_digest_flush_interval=45)
    monkeypatch.setattr("submissions_checker.core.config.get_settings", lambda: fake_settings)

    result = scheduler_module.init_scheduler()

    assert result is sched
    assert scheduler_module._scheduler is sched

    # Two jobs were registered: outbox processor (10s) + teacher digest (45s).
    assert sched.add_job.call_count == 2
    by_id = {c.kwargs["id"]: c for c in sched.add_job.call_args_list}
    assert set(by_id) == {"outbox_processor", "teacher_digest_processor"}

    outbox = by_id["outbox_processor"]
    assert outbox.kwargs["trigger"].interval.total_seconds() == 10
    assert outbox.kwargs["max_instances"] == 1

    digest = by_id["teacher_digest_processor"]
    assert digest.kwargs["trigger"].interval.total_seconds() == 45
    assert digest.kwargs["max_instances"] == 1


def test_init_scheduler_is_idempotent(monkeypatch) -> None:
    sched = _fake_scheduler()
    _install_fake_scheduler(monkeypatch, sched)
    monkeypatch.setattr(
        "submissions_checker.core.config.get_settings",
        lambda: MagicMock(teacher_digest_flush_interval=30),
    )

    first = scheduler_module.init_scheduler()
    add_job_calls = sched.add_job.call_count
    second = scheduler_module.init_scheduler()

    assert first is second
    # No new jobs added on the second init.
    assert sched.add_job.call_count == add_job_calls


async def test_start_scheduler_starts_when_not_running(monkeypatch) -> None:
    sched = _fake_scheduler()
    scheduler_module._scheduler = sched

    await scheduler_module.start_scheduler()

    sched.start.assert_called_once()


async def test_start_scheduler_noop_when_already_running(monkeypatch) -> None:
    sched = _fake_scheduler()
    sched.running = True
    scheduler_module._scheduler = sched

    await scheduler_module.start_scheduler()

    sched.start.assert_not_called()


async def test_shutdown_scheduler_shuts_down_when_running() -> None:
    sched = _fake_scheduler()
    sched.running = True
    scheduler_module._scheduler = sched

    await scheduler_module.shutdown_scheduler()

    sched.shutdown.assert_called_once_with(wait=True)


async def test_shutdown_scheduler_noop_when_not_initialized() -> None:
    scheduler_module._scheduler = None
    # Should not raise even though nothing is initialized.
    await scheduler_module.shutdown_scheduler()


async def test_shutdown_scheduler_noop_when_not_running() -> None:
    sched = _fake_scheduler()
    sched.running = False
    scheduler_module._scheduler = sched

    await scheduler_module.shutdown_scheduler()

    sched.shutdown.assert_not_called()
