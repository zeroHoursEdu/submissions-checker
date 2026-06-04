"""Transactional outbox pattern model for reliable event processing."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from submissions_checker.db.models.base import Base, TimestampMixin
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState


class OutboxMessage(Base, TimestampMixin):
    """
    Outbox message for transactional event processing.

    The transactional outbox pattern ensures reliable event processing:
    1. Business logic writes to database + outbox table in same transaction
    2. Background worker polls outbox table for unprocessed messages
    3. Worker dispatches messages to appropriate handlers
    4. Messages marked as processed on success, retry on failure
    """

    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Event information
    event_type: Mapped[OutboxEventType] = mapped_column(
        SQLEnum(OutboxEventType, name="outbox_event_type", native_enum=True),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Processing status
    state: Mapped[OutboxMessageState] = mapped_column(
        SQLEnum(
            OutboxMessageState,
            name="outbox_message_state",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=OutboxMessageState.PENDING,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Retry handling
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Composite index for efficient queries by state and event type
        Index("ix_outbox_messages_state_event_type", "state", "event_type"),
        # Index for efficient queries of pending messages ordered by creation time
        Index("ix_outbox_messages_pending", "state", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<OutboxMessage(id={self.id}, "
            f"event={self.event_type}, "
            f"state={self.state})>"
        )

    def mark_finished(self) -> None:
        """Mark the message as successfully finished."""
        self.state = OutboxMessageState.FINISHED
        self.finished_at = datetime.now(UTC)

    def mark_error(self, error: str) -> None:
        """Mark the message as failed and increment retry count for retry."""
        self.state = OutboxMessageState.ERROR
        self.retry_count += 1
        self.error_message = error
