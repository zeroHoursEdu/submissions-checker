"""Email notification channel using Resend API (https://resend.com)."""

import httpx

from submissions_checker.core.logging import get_logger
from submissions_checker.services.notifications.base import NotificationChannel

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


class ResendChannel(NotificationChannel):
    """Sends notifications via Resend HTTP API."""

    def __init__(self, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from_address = from_address

    async def send(self, recipient: str, subject: str, body: str) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                RESEND_API_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_address,
                    "to": [recipient],
                    "subject": subject,
                    "text": body,
                },
                timeout=15,
            )
            if response.is_error:
                # Resend explains the refusal in the body — an unverified sender
                # domain, or a recipient other than the account owner while no domain
                # is verified, both of which are 403. `raise_for_status()` alone
                # reports "403 Forbidden" and discards the one useful sentence, which
                # leaves an operator guessing at a configuration problem the API
                # already named. The body carries no credential; the key is only ever
                # sent in the request header.
                detail = response.text[:500]
                logger.error(
                    "resend_send_failed",
                    status_code=response.status_code,
                    from_address=self._from_address,
                    detail=detail,
                )
                raise httpx.HTTPStatusError(
                    f"Resend rejected the message ({response.status_code}): {detail}",
                    request=response.request,
                    response=response,
                )
