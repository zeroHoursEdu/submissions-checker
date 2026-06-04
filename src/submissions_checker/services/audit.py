"""Audit log helper — write immutable records of important actions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.audit_log import AuditLog


async def audit(
    db: AsyncSession,
    action: str,
    actor_id: int | None = None,
    actor_username: str | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    **detail: Any,
) -> None:
    """Append an audit log entry. Does not commit — caller owns the transaction."""
    db.add(AuditLog(
        actor_id=actor_id,
        actor_username=actor_username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    ))
