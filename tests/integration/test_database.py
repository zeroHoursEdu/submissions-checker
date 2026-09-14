"""Database integration tests."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState
from submissions_checker.db.models.outbox import OutboxMessage


@pytest.mark.asyncio
async def test_database_connection(db_session: AsyncSession) -> None:
    """Test basic database connectivity."""
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_create_outbox_message(db_session: AsyncSession) -> None:
    """Test creating an outbox message defaults to PENDING state."""
    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"key": "value"},
    )

    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    assert message.id is not None
    assert message.event_type == OutboxEventType.NEW_SUBMISSION
    assert message.payload == {"key": "value"}
    assert message.state == OutboxMessageState.PENDING
    assert message.retry_count == 0
    assert message.finished_at is None
    assert message.error_message is None


@pytest.mark.asyncio
async def test_outbox_message_mark_finished(db_session: AsyncSession) -> None:
    """Test marking an outbox message as finished."""
    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={},
    )

    db_session.add(message)
    await db_session.commit()

    message.mark_finished()
    await db_session.commit()
    await db_session.refresh(message)

    assert message.state == OutboxMessageState.FINISHED
    assert message.finished_at is not None


@pytest.mark.asyncio
async def test_outbox_message_mark_error(db_session: AsyncSession) -> None:
    """Test marking an outbox message as errored increments retry count."""
    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={},
    )

    db_session.add(message)
    await db_session.commit()

    error_msg = "Test error message"
    message.mark_error(error_msg)
    await db_session.commit()
    await db_session.refresh(message)

    assert message.state == OutboxMessageState.ERROR
    assert message.retry_count == 1
    assert message.error_message == error_msg
    assert message.finished_at is None


@pytest.mark.asyncio
async def test_outbox_message_mark_error_accumulates_retries(
    db_session: AsyncSession,
) -> None:
    """Test repeated errors keep incrementing retry_count."""
    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={},
    )
    db_session.add(message)
    await db_session.commit()

    message.mark_error("first")
    message.mark_error("second")
    await db_session.commit()
    await db_session.refresh(message)

    assert message.retry_count == 2
    assert message.error_message == "second"
    assert message.state == OutboxMessageState.ERROR


@pytest.mark.asyncio
async def test_query_pending_outbox_messages(db_session: AsyncSession) -> None:
    """Test querying pending (unprocessed) outbox messages."""
    # Finished message
    finished_msg = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"which": "finished"},
    )
    finished_msg.mark_finished()
    db_session.add(finished_msg)

    # Pending message (default state)
    pending_msg = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"which": "pending"},
    )
    db_session.add(pending_msg)

    await db_session.commit()

    result = await db_session.execute(
        select(OutboxMessage).where(OutboxMessage.state == OutboxMessageState.PENDING)
    )
    messages = result.scalars().all()

    assert len(messages) == 1
    assert messages[0].payload == {"which": "pending"}
