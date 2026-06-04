"""Notification dispatcher: fans out to all configured channels."""

from submissions_checker.services.notifications.base import NotificationChannel
from submissions_checker.services.notifications.brevo_channel import BrevoChannel
from submissions_checker.services.notifications.email import EmailChannel
from submissions_checker.services.notifications.resend_channel import ResendChannel


class NotificationDispatcher:
    """Delivers a notification through all configured channels."""

    def __init__(self, channels: list[NotificationChannel]) -> None:
        self._channels = channels

    async def notify(self, recipient: str, subject: str, body: str) -> None:
        """Send notification to recipient via every configured channel."""
        for channel in self._channels:
            await channel.send(recipient, subject, body)


def build_dispatcher(settings: object) -> NotificationDispatcher:
    """Build a NotificationDispatcher from application settings.

    Channels are opt-in: a channel is only added when its required config is present.
    To add a new channel, instantiate it here and append to `channels`.
    """
    channels: list[NotificationChannel] = []

    resend_api_key = getattr(settings, "resend_api_key", None)
    if resend_api_key:
        channels.append(
            ResendChannel(
                api_key=resend_api_key,
                from_address=getattr(settings, "resend_from_address", "noreply@example.com"),
            )
        )

    brevo_api_key = getattr(settings, "brevo_api_key", None)
    if brevo_api_key:
        channels.append(
            BrevoChannel(
                api_key=brevo_api_key,
                from_address=getattr(settings, "brevo_from_address", "noreply@example.com"),
            )
        )

    smtp_host = getattr(settings, "smtp_host", None)
    if smtp_host:
        channels.append(
            EmailChannel(
                host=smtp_host,
                port=getattr(settings, "smtp_port", 587),
                username=getattr(settings, "smtp_username", None),
                password=getattr(settings, "smtp_password", None),
                from_address=getattr(settings, "smtp_from_address", "noreply@example.com"),
                use_tls=getattr(settings, "smtp_use_tls", True),
            )
        )

    return NotificationDispatcher(channels)
