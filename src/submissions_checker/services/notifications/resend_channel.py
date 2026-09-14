"""Email notification channel using Resend API (https://resend.com)."""

import httpx

from submissions_checker.services.notifications.base import NotificationChannel

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
            response.raise_for_status()
