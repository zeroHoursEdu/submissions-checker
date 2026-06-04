"""In-app notification helper."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.notification import Notification


async def push_notification(
    db: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    link: str | None = None,
) -> None:
    """Create an in-app notification. Does not commit — caller owns the transaction."""
    db.add(Notification(user_id=user_id, title=title, body=body, link=link))
