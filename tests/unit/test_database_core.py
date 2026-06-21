"""Unit tests for the core database engine/session glue.

The async engine and sessionmaker are mocked so nothing connects to a real
database. The tests cover the singleton caching of ``get_engine`` /
``get_session_factory``, the ``get_db`` FastAPI dependency commit/rollback/close
contract, ``init_db`` warming the singletons, and ``close_db`` disposing +
resetting them. ``db.session.get_session`` is covered separately in
``test_db_session.py`` — these target ``core.database`` only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from submissions_checker.core import database as db_module


@pytest.fixture(autouse=True)
def _reset_globals():
    """Each test starts and ends with the module-level singletons cleared."""
    db_module._engine = None
    db_module._async_session_factory = None
    yield
    db_module._engine = None
    db_module._async_session_factory = None


def test_get_engine_creates_once_and_caches(monkeypatch) -> None:
    created = MagicMock(name="engine")
    ctor = MagicMock(return_value=created)
    monkeypatch.setattr(db_module, "create_async_engine", ctor)
    monkeypatch.setattr(
        db_module, "get_settings",
        lambda: MagicMock(database_url="postgresql+asyncpg://x", debug=False),
    )

    first = db_module.get_engine()
    second = db_module.get_engine()

    assert first is created
    assert second is created
    # Engine constructed exactly once (singleton).
    ctor.assert_called_once()
    _, kwargs = ctor.call_args
    assert kwargs["pool_size"] == 5
    assert kwargs["max_overflow"] == 10
    assert kwargs["pool_pre_ping"] is True


def test_get_session_factory_creates_once_and_caches(monkeypatch) -> None:
    fake_engine = MagicMock(name="engine")
    monkeypatch.setattr(db_module, "get_engine", lambda: fake_engine)
    maker = MagicMock(return_value=MagicMock(name="factory"))
    monkeypatch.setattr(db_module, "async_sessionmaker", maker)

    first = db_module.get_session_factory()
    second = db_module.get_session_factory()

    assert first is second
    maker.assert_called_once()
    args, kwargs = maker.call_args
    assert args[0] is fake_engine
    assert kwargs["expire_on_commit"] is False


async def test_get_db_commits_and_closes_on_success(monkeypatch) -> None:
    session = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = False
    monkeypatch.setattr(
        db_module, "get_session_factory", lambda: MagicMock(return_value=cm)
    )

    agen = db_module.get_db()
    yielded = await agen.__anext__()
    assert yielded is session
    # Drive the generator to completion (StopAsyncIteration) -> commit + close.
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    session.commit.assert_awaited_once()
    session.rollback.assert_not_called()
    session.close.assert_awaited_once()


async def test_get_db_rolls_back_and_reraises_on_exception(monkeypatch) -> None:
    session = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = False
    monkeypatch.setattr(
        db_module, "get_session_factory", lambda: MagicMock(return_value=cm)
    )

    agen = db_module.get_db()
    await agen.__anext__()
    with pytest.raises(ValueError, match="boom"):
        await agen.athrow(ValueError("boom"))

    session.commit.assert_not_called()
    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_init_db_warms_singletons(monkeypatch) -> None:
    engine = MagicMock(name="engine")
    monkeypatch.setattr(db_module, "get_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(
        db_module, "get_session_factory", MagicMock(return_value=MagicMock())
    )

    await db_module.init_db()

    db_module.get_engine.assert_called_once()
    db_module.get_session_factory.assert_called_once()


async def test_close_db_disposes_and_resets(monkeypatch) -> None:
    engine = AsyncMock(name="engine")
    db_module._engine = engine
    db_module._async_session_factory = MagicMock()

    await db_module.close_db()

    engine.dispose.assert_awaited_once()
    assert db_module._engine is None
    assert db_module._async_session_factory is None


async def test_close_db_noop_when_no_engine() -> None:
    db_module._engine = None
    db_module._async_session_factory = None
    # Must not raise when there is nothing to dispose.
    await db_module.close_db()
    assert db_module._engine is None
