"""Task: send account credentials email to a newly registered student."""

from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import credentials_template

logger = get_logger(__name__)


async def execute_send_credentials_task(db: AsyncSession, payload: dict) -> None:
    """Send login credentials to a newly registered student.

    Payload keys:
        student_email: recipient address
        full_name: student's display name
        username: generated login username
        password: plaintext password (transmitted once, then discarded from outbox)

    If no email channel is configured, the task completes silently — the outbox
    record serves as an audit trail that credentials were issued.
    """
    settings = get_settings()
    student_email: str = payload["student_email"]
    full_name: str = payload["full_name"]
    username: str = payload["username"]
    password: str = payload["password"]

    login_url = f"{settings.app_base_url.rstrip('/')}/auth/login"

    subject, body = credentials_template(full_name, username, password, login_url)

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning(
            "send_credentials_no_channel_configured",
            student_email=student_email,
            message="No email provider configured; credentials not delivered",
        )
        return

    await dispatcher.notify(student_email, subject, body)
    logger.info("send_credentials_sent", student_email=student_email, username=username)
