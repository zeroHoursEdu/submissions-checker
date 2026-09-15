"""The gauge refresh: sets gauges from the database, flips app_db_healthy on failure."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from submissions_checker.core import metrics
from submissions_checker.workers.scheduled import metrics_refresh

VALUES = {
    "students_total": 42,
    "students_active_1d": 3,
    "students_active_7d": 10,
    "students_active_30d": 20,
    "quiz_attempts_in_progress": 2,
    "submissions_awaiting_teacher_review": 5,
    "disputes_open": 1,
    "outbox_pending": 4,
    "outbox_error": 0,
    "outbox_oldest_pending_age_seconds": 12.5,
}


def _fake_session_factory(monkeypatch) -> None:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(metrics_refresh, "get_session_factory", lambda: lambda: session)


def _fake_engine(monkeypatch, *, checked_out: int = 3, size: int = 4, overflow: int = 1) -> None:
    pool = MagicMock(checkedout=lambda: checked_out, size=lambda: size, overflow=lambda: overflow)
    monkeypatch.setattr(metrics_refresh, "get_engine", lambda: MagicMock(pool=pool))


@pytest.mark.asyncio
async def test_refresh_sets_gauges_and_marks_db_healthy(monkeypatch) -> None:
    monkeypatch.setattr(metrics_refresh, "compute_gauges", AsyncMock(return_value=dict(VALUES)))
    _fake_session_factory(monkeypatch)
    _fake_engine(monkeypatch)

    await metrics_refresh.refresh_metrics()

    assert metrics.students_total._value.get() == 42
    assert metrics.students_active.labels(window="7d")._value.get() == 10
    assert metrics.quiz_attempts_in_progress._value.get() == 2
    assert metrics.submissions_awaiting_teacher_review._value.get() == 5
    assert metrics.disputes_open._value.get() == 1
    assert metrics.outbox_pending._value.get() == 4
    assert metrics.outbox_oldest_pending_age_seconds._value.get() == 12.5
    assert metrics.db_pool_checked_out._value.get() == 3
    assert metrics.db_pool_size._value.get() == 4 + 1
    assert metrics.app_db_healthy._value.get() == 1


@pytest.mark.asyncio
async def test_refresh_marks_db_unhealthy_and_keeps_old_values_on_error(monkeypatch) -> None:
    metrics.students_total.set(7)
    monkeypatch.setattr(
        metrics_refresh, "compute_gauges", AsyncMock(side_effect=RuntimeError("pg down"))
    )
    _fake_session_factory(monkeypatch)
    _fake_engine(monkeypatch, checked_out=0, overflow=-4)

    await metrics_refresh.refresh_metrics()  # must not raise

    assert metrics.app_db_healthy._value.get() == 0
    assert metrics.students_total._value.get() == 7
    # SQLAlchemy reports negative overflow when idle; the gauge never goes below pool size.
    assert metrics.db_pool_size._value.get() == 4
