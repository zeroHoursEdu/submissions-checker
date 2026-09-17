"""Task: send account credentials email to a newly registered student."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.core.sealed import unseal
from submissions_checker.services.notifications.dispatcher import build_dispatcher
from submissions_checker.services.notifications.templates import credentials_template

logger = get_logger(__name__)


async def execute_send_credentials_task(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Send login credentials to a newly registered student.

    Payload keys:
        student_email: recipient address
        full_name: student's display name
        username: generated login username
        password_sealed: the password, sealed with the app key (see core.sealed). It is
            opened in memory here and the dispatcher overwrites it with a placeholder
            once this task returns, so the row never keeps a usable credential.

    If no email channel is configured, the task completes silently — the outbox
    record serves as an audit trail that credentials were issued.
    """
    settings = get_settings()
    student_email: str = payload["student_email"]
    full_name: str = payload["full_name"]
    username: str = payload["username"]
    password = _password_from(payload)

    login_url = f"{settings.app_base_url.rstrip('/')}/auth/login"

    subject, body = credentials_template(full_name, username, password, login_url)

    dispatcher = build_dispatcher(settings)
    if not dispatcher._channels:
        logger.warning(
            "send_credentials_no_channel_configured",
            username=username,
            message="No email provider configured; credentials not delivered",
        )
        return

    await dispatcher.notify(student_email, subject, body)
    logger.info("send_credentials_sent", username=username)


REDACTED = "<sent>"
_SECRET_KEYS = ("password_sealed", "password")


def _password_from(payload: dict[str, Any]) -> str:
    """The password to deliver: sealed by every current writer, plain in rows that
    predate sealing. A seal that cannot be opened raises, which fails the message
    into retry rather than sending an e-mail with nothing in it."""
    sealed_value = payload.get("password_sealed")
    if sealed_value is not None:
        return unseal(str(sealed_value))
    return str(payload["password"])


def redact_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with every form of the password replaced by a placeholder.

    Returns a new dict on purpose: assigning it to the row is what makes SQLAlchemy
    notice the JSONB column changed."""
    return {k: (REDACTED if k in _SECRET_KEYS else v) for k, v in payload.items()}
