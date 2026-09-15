"""Worker integration tests."""

from contextlib import asynccontextmanager

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core import metrics
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.workers.scheduled import outbox_processor


@pytest.mark.asyncio
async def test_worker_redis_connectivity(redis_container) -> None:
    """The Redis test container is reachable and round-trips a value."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    client = aioredis.from_url(f"redis://{host}:{port}/0")
    try:
        assert await client.ping() is True
        await client.set("worker:probe", "ok")
        assert await client.get("worker:probe") == b"ok"
    finally:
        await client.aclose()


def _patch_processor_session(monkeypatch, db_session: AsyncSession) -> None:
    """Route the processor's get_session() at the test's session."""

    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(outbox_processor, "get_session", fake_get_session)


@pytest.mark.asyncio
async def test_outbox_processor_marks_message_finished(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A dispatchable message is marked FINISHED after processing."""
    dispatched: list[int] = []

    async def fake_dispatch(db, message):
        dispatched.append(message.id)

    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"submission_id": 1},
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)
    monkeypatch.setattr(outbox_processor, "dispatch_outbox_message", fake_dispatch)
    finished_before = metrics.outbox_processed_total.labels(
        event_type="NEW_SUBMISSION", outcome="finished"
    )._value.get()

    await outbox_processor.process_outbox_messages()

    await db_session.refresh(message)
    assert dispatched == [message.id]
    assert (
        metrics.outbox_processed_total.labels(
            event_type="NEW_SUBMISSION", outcome="finished"
        )._value.get()
        == finished_before + 1
    )
    assert message.state == OutboxMessageState.FINISHED
    assert message.finished_at is not None

    # No PENDING messages remain.
    pending = (
        (
            await db_session.execute(
                select(OutboxMessage).where(OutboxMessage.state == OutboxMessageState.PENDING)
            )
        )
        .scalars()
        .all()
    )
    assert pending == []


@pytest.mark.asyncio
async def test_outbox_processor_marks_message_error_on_failure(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A failing dispatch marks the message ERROR and records the error."""

    async def failing_dispatch(db, message):
        raise ValueError("boom")

    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"submission_id": 2},
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)
    monkeypatch.setattr(outbox_processor, "dispatch_outbox_message", failing_dispatch)
    error_before = metrics.outbox_processed_total.labels(
        event_type="NEW_SUBMISSION", outcome="error"
    )._value.get()

    await outbox_processor.process_outbox_messages()

    await db_session.refresh(message)
    assert message.state == OutboxMessageState.ERROR
    assert (
        metrics.outbox_processed_total.labels(
            event_type="NEW_SUBMISSION", outcome="error"
        )._value.get()
        == error_before + 1
    )
    assert message.retry_count == 1
    assert message.error_message == "boom"


@pytest.mark.asyncio
async def test_outbox_processor_drops_retired_event_type_without_retry(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A retired/legacy event type (docs/known_bugs.md #8) is marked ERROR and its
    retry budget is pre-exhausted on the first attempt, instead of retrying
    outbox_max_retries times against the same undispatchable event before going
    silent.

    This exercises the real dispatch routing table (no dispatch monkeypatch): the
    deprecated PULL event has no handler and must be dropped immediately.
    """
    from submissions_checker.core.config import get_settings

    message = OutboxMessage(
        event_type=OutboxEventType.PULL,
        payload={},
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)

    await outbox_processor.process_outbox_messages()

    await db_session.refresh(message)
    assert message.state == OutboxMessageState.ERROR
    assert message.retry_count >= get_settings().outbox_max_retries
    assert "Retired event type" in (message.error_message or "")

    # Excluded from the next poll's retry_count < outbox_max_retries filter.
    pending = (
        (
            await db_session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.state.in_([OutboxMessageState.PENDING, OutboxMessageState.ERROR]),
                    OutboxMessage.retry_count < get_settings().outbox_max_retries,
                )
            )
        )
        .scalars()
        .all()
    )
    assert message not in pending


@pytest.mark.asyncio
async def test_dispatch_still_errors_on_a_truly_unhandled_event_type() -> None:
    """The generic unknown-type branch (distinct from the retired-type branch)
    still raises for an event type that is neither dispatched nor retired."""
    from types import SimpleNamespace

    from submissions_checker.workers.scheduled.outbox_processor import (
        dispatch_outbox_message,
    )

    message = SimpleNamespace(id=1, event_type=SimpleNamespace(value="SOMETHING_ELSE"))
    with pytest.raises(ValueError, match="Unknown event type"):
        await dispatch_outbox_message(db=None, message=message)
