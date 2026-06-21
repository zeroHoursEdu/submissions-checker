"""Unit tests for the ``get_session()`` async context manager.

The real session factory is replaced with a mock returning an ``AsyncMock``
session, so no database is touched. The tests assert the commit/rollback/close
contract of ``db.session.get_session`` on both the happy and exception paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from submissions_checker.db import session as session_module


def _patch_factory(monkeypatch) -> AsyncMock:
    """Install a fake session factory; return the AsyncMock session it yields."""
    fake_session = AsyncMock()

    # ``async with session_factory() as session`` -> the factory call returns an
    # async context manager whose __aenter__ yields ``fake_session``.
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = False

    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(session_module, "get_session_factory", lambda: factory)
    return fake_session


async def test_get_session_commits_and_closes_on_success(monkeypatch) -> None:
    """Happy path: the session is committed then closed, never rolled back."""
    fake_session = _patch_factory(monkeypatch)

    async with session_module.get_session() as session:
        assert session is fake_session

    fake_session.commit.assert_awaited_once()
    fake_session.rollback.assert_not_called()
    fake_session.close.assert_awaited_once()


async def test_get_session_rolls_back_and_reraises_on_exception(monkeypatch) -> None:
    """Exception path: rollback is awaited, the error re-raises, no commit."""
    fake_session = _patch_factory(monkeypatch)

    with pytest.raises(ValueError, match="boom"):
        async with session_module.get_session():
            raise ValueError("boom")

    fake_session.commit.assert_not_called()
    fake_session.rollback.assert_awaited_once()
    fake_session.close.assert_awaited_once()


async def test_get_session_closes_even_when_commit_fails(monkeypatch) -> None:
    """If commit itself raises, it is treated as an error: rollback + close run."""
    fake_session = _patch_factory(monkeypatch)
    fake_session.commit.side_effect = RuntimeError("commit failed")

    with pytest.raises(RuntimeError, match="commit failed"):
        async with session_module.get_session():
            pass

    fake_session.commit.assert_awaited_once()
    fake_session.rollback.assert_awaited_once()
    fake_session.close.assert_awaited_once()
