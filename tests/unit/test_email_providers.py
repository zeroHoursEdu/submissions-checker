"""Unit tests for the Brevo and Resend HTTP email channels.

``httpx.AsyncClient`` is patched so no network call is made. The tests assert
the POST URL, auth headers, and JSON body (from/to/subject/text), plus that
HTTP errors propagate via ``raise_for_status``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from submissions_checker.services.notifications.brevo_channel import (
    BREVO_API_URL,
    BrevoChannel,
)
from submissions_checker.services.notifications.resend_channel import (
    RESEND_API_URL,
    ResendChannel,
)


def _patch_httpx(module_path: str, *, raise_for_status_exc: Exception | None = None):
    """Patch ``httpx.AsyncClient`` in *module_path*; return (cm, mock_client, response).

    The context manager replaces the AsyncClient so ``async with httpx.AsyncClient()``
    yields ``mock_client`` whose ``.post`` returns ``response``.
    """
    response = MagicMock()
    # Brevo still calls raise_for_status; Resend inspects `is_error` so that it can
    # include the provider's explanation in the error it raises.
    response.is_error = raise_for_status_exc is not None
    response.status_code = 403 if raise_for_status_exc is not None else 200
    response.text = ""
    if raise_for_status_exc is not None:
        response.raise_for_status.side_effect = raise_for_status_exc
    else:
        response.raise_for_status.return_value = None

    client = AsyncMock()
    client.post.return_value = response

    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_cm.__aexit__.return_value = False

    cm = patch(f"{module_path}.httpx.AsyncClient", return_value=client_cm)
    return cm, client, response


# ── Brevo ──────────────────────────────────────────────────────────────────────


async def test_brevo_send_posts_expected_request() -> None:
    channel = BrevoChannel(api_key="brevo-key", from_address="noreply@x.com")
    module = "submissions_checker.services.notifications.brevo_channel"
    cm, client, response = _patch_httpx(module)

    with cm:
        await channel.send("ada@x.com", "Subject line", "Hello body")

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == BREVO_API_URL
    assert kwargs["headers"] == {"api-key": "brevo-key"}
    assert kwargs["json"] == {
        "sender": {"email": "noreply@x.com"},
        "to": [{"email": "ada@x.com"}],
        "subject": "Subject line",
        "textContent": "Hello body",
    }
    response.raise_for_status.assert_called_once()


async def test_brevo_send_propagates_http_error() -> None:
    channel = BrevoChannel(api_key="k", from_address="f@x.com")
    module = "submissions_checker.services.notifications.brevo_channel"
    err = httpx.HTTPStatusError("400", request=MagicMock(), response=MagicMock())
    cm, _client, _response = _patch_httpx(module, raise_for_status_exc=err)

    with cm, pytest.raises(httpx.HTTPStatusError):
        await channel.send("a@x.com", "s", "b")


# ── Resend ─────────────────────────────────────────────────────────────────────


async def test_resend_send_posts_expected_request() -> None:
    channel = ResendChannel(api_key="resend-key", from_address="noreply@x.com")
    module = "submissions_checker.services.notifications.resend_channel"
    cm, client, response = _patch_httpx(module)

    with cm:
        await channel.send("ada@x.com", "Subject line", "Hello body")

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == RESEND_API_URL
    assert kwargs["headers"] == {"Authorization": "Bearer resend-key"}
    assert kwargs["json"] == {
        "from": "noreply@x.com",
        "to": ["ada@x.com"],
        "subject": "Subject line",
        "text": "Hello body",
    }


async def test_resend_send_propagates_http_error() -> None:
    channel = ResendChannel(api_key="k", from_address="f@x.com")
    module = "submissions_checker.services.notifications.resend_channel"
    err = httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock())
    cm, _client, _response = _patch_httpx(module, raise_for_status_exc=err)

    with cm, pytest.raises(httpx.HTTPStatusError):
        await channel.send("a@x.com", "s", "b")


async def test_resend_error_carries_the_providers_explanation() -> None:
    """A 403 alone is not actionable; the body names the actual misconfiguration.

    Resend answers 403 both for an unverified sender domain and for a recipient
    other than the account owner. Reporting only the status code leaves an operator
    guessing between them.
    """
    channel = ResendChannel(api_key="k", from_address="noreply@example.edu")
    module = "submissions_checker.services.notifications.resend_channel"
    err = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())
    cm, _client, response = _patch_httpx(module, raise_for_status_exc=err)
    response.text = '{"statusCode":403,"message":"The example.edu domain is not verified."}'

    with cm, pytest.raises(httpx.HTTPStatusError) as excinfo:
        await channel.send("a@x.com", "s", "b")

    assert "not verified" in str(excinfo.value)
    assert "403" in str(excinfo.value)
