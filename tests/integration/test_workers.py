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


@pytest.mark.asyncio
async def test_outbox_failure_is_logged_with_message_context(
    db_session: AsyncSession, monkeypatch
) -> None:
    import structlog
    from structlog.testing import capture_logs

    from submissions_checker.core.logging import get_logger

    inner = get_logger("t.task")

    async def failing_dispatch(db, message):
        inner.info("inside_task")
        raise ValueError("boom")

    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": 77})
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)
    monkeypatch.setattr(outbox_processor, "dispatch_outbox_message", failing_dispatch)
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await outbox_processor.process_outbox_messages()

    inside = next(e for e in logs if e["event"] == "inside_task")
    assert inside["outbox_id"] == message.id and inside["submission_id"] == 77
    assert inside["attempt_no"] == 1
    failed = next(e for e in logs if e["event"] == "outbox_failed")
    assert failed["log_level"] == "error" and failed["exc_info"]
    # Nothing leaks into the next job's lines.
    assert "outbox_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_outbox_last_retry_is_logged_as_dead(db_session: AsyncSession, monkeypatch) -> None:
    from structlog.testing import capture_logs

    from submissions_checker.core.config import get_settings

    async def failing_dispatch(db, message):
        raise ValueError("boom")

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS,
        payload={"submission_id": 78},
        retry_count=get_settings().outbox_max_retries - 1,
    )
    db_session.add(message)
    await db_session.commit()

    _patch_processor_session(monkeypatch, db_session)
    monkeypatch.setattr(outbox_processor, "dispatch_outbox_message", failing_dispatch)
    with capture_logs() as logs:
        await outbox_processor.process_outbox_messages()
    assert any(e["event"] == "outbox_dead" for e in logs)


@pytest.mark.asyncio
async def test_idle_outbox_tick_logs_nothing_at_info(db_session: AsyncSession, monkeypatch) -> None:
    from structlog.testing import capture_logs

    _patch_processor_session(monkeypatch, db_session)
    with capture_logs() as logs:
        await outbox_processor.process_outbox_messages()
    assert [e for e in logs if e["log_level"] != "debug"] == []


@pytest.mark.asyncio
async def test_outbox_failure_from_check_task_keeps_submission_id(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Regression: bind/unbind inside execute_check_task must not blow away the
    submission_id the outbox loop's own bound_contextvars put in place — the
    unbind used to leave the outbox's own outbox_failed line with no submission_id."""
    import structlog
    from structlog.testing import capture_logs

    from submissions_checker.workers.tasks import check_tasks

    async def failing_execute_check(db, submission_id):
        raise ValueError("boom")

    monkeypatch.setattr(check_tasks, "_execute_check", failing_execute_check)

    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": 77})
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await outbox_processor.process_outbox_messages()

    failed = next(e for e in logs if e["event"] == "outbox_failed")
    assert failed["submission_id"] == 77


@pytest.mark.asyncio
async def test_outbox_failure_from_ai_review_task_keeps_submission_id(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Same regression as above, for the RUN_AI_REVIEW path."""
    import structlog
    from structlog.testing import capture_logs

    from submissions_checker.workers.tasks import review_tasks

    async def failing_execute_ai_review(db, payload, submission_id):
        raise ValueError("boom")

    monkeypatch.setattr(review_tasks, "_execute_ai_review", failing_execute_ai_review)

    message = OutboxMessage(event_type=OutboxEventType.RUN_AI_REVIEW, payload={"submission_id": 78})
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    _patch_processor_session(monkeypatch, db_session)
    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await outbox_processor.process_outbox_messages()

    failed = next(e for e in logs if e["event"] == "outbox_failed")
    assert failed["submission_id"] == 78
