"""The real app returns X-Request-ID and binds the authenticated user."""

from __future__ import annotations

import pytest
import structlog
from httpx import AsyncClient
from structlog.testing import capture_logs

pytestmark = pytest.mark.asyncio


async def test_authenticated_request_is_logged_with_user(
    student_client: AsyncClient, student_user
) -> None:
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        resp = await student_client.get("/portal")
    assert resp.headers.get("x-request-id")
    access = [e for e in logs if e["event"] == "http_request"][-1]
    assert access["user_id"] == student_user.id
    assert access["role"] == "STUDENT"
    assert access["request_id"] == resp.headers["x-request-id"]
