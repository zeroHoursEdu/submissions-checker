"""The UI ships one language; the selector and ``/set-language`` were removed."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_set_language_route_is_gone(client: AsyncClient) -> None:
    resp = await client.post("/set-language", data={"lang": "uk"})
    assert resp.status_code == 404
