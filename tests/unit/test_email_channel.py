"""Unit tests for EmailChannel.

aiosmtplib.send is patched so no SMTP connection is made. Tests assert the
EmailMessage is constructed correctly (From/To/Subject/body) and that the SMTP
connection params (host, port, credentials, start_tls) are forwarded.

The "smtp-host-unset disabled" path lives in dispatcher.build_dispatcher (an
EmailChannel is only created when smtp_host is set); that is covered in
test_notifications.py. EmailChannel itself always sends when called.
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest

from submissions_checker.services.notifications.email import EmailChannel


def _channel(**overrides) -> EmailChannel:
    base = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "user",
        "password": "pass",
        "from_address": "noreply@example.com",
        "use_tls": True,
    }
    base.update(overrides)
    return EmailChannel(**base)


async def test_send_builds_message_and_forwards_smtp_params() -> None:
    channel = _channel()
    with patch(
        "submissions_checker.services.notifications.email.aiosmtplib.send",
        new=AsyncMock(),
    ) as send:
        await channel.send("ada@x.com", "Subject line", "Hello body")

    send.assert_awaited_once()
    args, kwargs = send.call_args
    msg = args[0]
    assert isinstance(msg, EmailMessage)
    assert msg["From"] == "noreply@example.com"
    assert msg["To"] == "ada@x.com"
    assert msg["Subject"] == "Subject line"
    assert msg.get_content().strip() == "Hello body"

    assert kwargs == {
        "hostname": "smtp.example.com",
        "port": 587,
        "username": "user",
        "password": "pass",
        "start_tls": True,
    }


async def test_send_without_credentials_passes_none() -> None:
    channel = _channel(username=None, password=None, use_tls=False, port=25)
    with patch(
        "submissions_checker.services.notifications.email.aiosmtplib.send",
        new=AsyncMock(),
    ) as send:
        await channel.send("bo@x.com", "s", "b")

    _, kwargs = send.call_args
    assert kwargs["username"] is None
    assert kwargs["password"] is None
    assert kwargs["start_tls"] is False
    assert kwargs["port"] == 25


async def test_send_propagates_smtp_errors() -> None:
    channel = _channel()
    with patch(
        "submissions_checker.services.notifications.email.aiosmtplib.send",
        new=AsyncMock(side_effect=ConnectionError("smtp down")),
    ):
        with pytest.raises(ConnectionError):
            await channel.send("a@x.com", "s", "b")
