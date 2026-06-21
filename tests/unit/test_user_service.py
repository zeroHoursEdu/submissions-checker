"""Contract tests for the (still-skeleton) UserService.

UserService is a documented skeleton: every method raises NotImplementedError.
These tests pin that contract so that, once real logic lands, the stubs are
forced to be revisited. The DB session is a mock — no real database is touched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from submissions_checker.services.user_service import UserService


@pytest.fixture
def service() -> UserService:
    # AsyncMock stands in for the AsyncSession; the skeleton never touches it.
    return UserService(db=AsyncMock())


def test_init_stores_db_session() -> None:
    db = AsyncMock()
    svc = UserService(db=db)
    assert svc.db is db


async def test_create_user_not_implemented(service: UserService) -> None:
    with pytest.raises(NotImplementedError):
        await service.create_user(email="a@b.com", username="ada")


async def test_get_user_by_id_not_implemented(service: UserService) -> None:
    with pytest.raises(NotImplementedError):
        await service.get_user_by_id(1)


async def test_get_user_by_username_not_implemented(service: UserService) -> None:
    with pytest.raises(NotImplementedError):
        await service.get_user_by_username("ada")


async def test_skeleton_does_not_touch_db(service: UserService) -> None:
    # NOTE: confirms the skeleton is inert — it must not issue any DB call yet.
    with pytest.raises(NotImplementedError):
        await service.create_user(email="a@b.com", username="ada")
    service.db.execute.assert_not_called()
    service.db.commit.assert_not_called()
    service.db.add.assert_not_called()
