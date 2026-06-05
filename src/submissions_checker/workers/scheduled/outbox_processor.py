"""Transactional outbox message processor."""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.session import get_session
from submissions_checker.workers.tasks.check_tasks import execute_check_task
from submissions_checker.workers.tasks.notification_tasks import (
    execute_deadline_reminder_task,
    execute_feedback_request_task,
    execute_new_submission_task,
    execute_quiz_result_task,
    execute_submission_reviewed_task,
)
from submissions_checker.workers.tasks.pull_tasks import execute_pull_task
from submissions_checker.workers.tasks.review_tasks import execute_review_task, execute_ai_review_task
from submissions_checker.workers.tasks.notify_tasks import execute_notify_task
from submissions_checker.workers.tasks.send_credentials_tasks import execute_send_credentials_task

logger = get_logger(__name__)

# PostgreSQL advisory lock ID for outbox processing
# Using a large prime number to avoid collision with other locks
OUTBOX_PROCESSOR_LOCK_ID = 7919  # Prime number for lock identification


async def process_outbox_messages() -> None:
    """
    Process pending outbox messages (scheduled job).

    This function runs periodically (every 10 seconds) to:
    1. Acquire PostgreSQL advisory lock (ensures single processor)
    2. Query pending and error messages (with retry limits)
    3. Dispatch messages to appropriate background tasks
    4. Mark messages as FINISHED on success
    5. Mark messages as ERROR on failure (for retry)
    6. Release advisory lock

    The transactional outbox pattern ensures reliable event processing:
    - Business logic writes to database + outbox table in same transaction
    - This job polls for pending/error messages
    - Messages are dispatched to background tasks (via asyncio.create_task)
    - Processing is idempotent and handles retries

    Advisory lock ensures only one processor runs at a time across all instances.
    """
    settings = get_settings()
    logger.info("process_outbox_messages_started")

    finished_count = 0
    error_count = 0

    try:
        async with get_session() as db:
            # Try to acquire PostgreSQL advisory lock (non-blocking)
            # This ensures only one outbox processor runs at a time
            lock_result = await db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": OUTBOX_PROCESSOR_LOCK_ID}
            )
            lock_acquired = lock_result.scalar()

            if not lock_acquired:
                logger.info(
                    "outbox_processor_lock_not_acquired",
                    message="Another processor is already running, skipping this execution"
                )
                return

            logger.debug("outbox_processor_lock_acquired", lock_id=OUTBOX_PROCESSOR_LOCK_ID)

            try:
                # Query pending and error messages (with retry limit)
                # Process both PENDING and ERROR states to retry failed messages
                result = await db.execute(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.state.in_([OutboxMessageState.PENDING, OutboxMessageState.ERROR])
                    )
                    .where(OutboxMessage.retry_count < settings.outbox_max_retries)
                    .order_by(OutboxMessage.created_at.asc())
                    .limit(settings.outbox_batch_size)
                )
                messages = result.scalars().all()

                logger.info("outbox_messages_fetched", count=len(messages))

                for message in messages:
                    try:
                        # Dispatch message to appropriate task based on event type
                        # Pass db session for transactional execution
                        await dispatch_outbox_message(db, message)

                        # Mark as finished
                        message.mark_finished()
                        finished_count += 1

                    except Exception as e:
                        logger.error(
                            "outbox_message_dispatch_failed",
                            message_id=message.id,
                            event_type=message.event_type.value,
                            state=message.state.value,
                            retry_count=message.retry_count,
                            error=str(e),
                        )

                        # Mark as error and increment retry count
                        message.mark_error(str(e))
                        error_count += 1

                # Commit all changes (finished and error messages)
                await db.commit()

                logger.info(
                    "process_outbox_messages_completed",
                    finished=finished_count,
                    error=error_count,
                )

            finally:
                # Always release the advisory lock
                await db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": OUTBOX_PROCESSOR_LOCK_ID}
                )
                logger.debug("outbox_processor_lock_released", lock_id=OUTBOX_PROCESSOR_LOCK_ID)

    except Exception as e:
        logger.error("process_outbox_messages_error", error=str(e))


async def dispatch_outbox_message(db: AsyncSession, message: OutboxMessage) -> None:
    """
    Dispatch an outbox message to the appropriate task handler.

    This function executes tasks synchronously (awaiting completion) within the
    database transaction, ensuring that task side effects (creating Submission
    records, creating REVIEW messages) are committed atomically with the PULL
    message state change.

    Args:
        db: Database session for transactional operations
        message: Outbox message to dispatch

    Raises:
        Exception: If dispatch fails
    """
    logger.info(
        "dispatching_outbox_message",
        message_id=message.id,
        event_type=message.event_type.value,
    )

    # Route messages to appropriate tasks based on event type
    # Using await (not asyncio.create_task) to ensure transactional consistency
    if message.event_type == OutboxEventType.PULL:
        await execute_pull_task(db, message.payload)

    elif message.event_type == OutboxEventType.REVIEW:
        await execute_review_task(db, message.payload)

    elif message.event_type == OutboxEventType.NOTIFY:
        await execute_notify_task(db, message.payload)

    elif message.event_type == OutboxEventType.SEND_CREDENTIALS:
        await execute_send_credentials_task(db, message.payload)

    elif message.event_type == OutboxEventType.SUBMISSION_REVIEWED:
        await execute_submission_reviewed_task(db, message.payload)

    elif message.event_type == OutboxEventType.QUIZ_RESULT:
        await execute_quiz_result_task(db, message.payload)

    elif message.event_type == OutboxEventType.DEADLINE_REMINDER:
        await execute_deadline_reminder_task(db, message.payload)

    elif message.event_type == OutboxEventType.NEW_SUBMISSION:
        await execute_new_submission_task(db, message.payload)

    elif message.event_type == OutboxEventType.RUN_CHECKS:
        await execute_check_task(db, message.payload)

    elif message.event_type == OutboxEventType.RUN_AI_REVIEW:
        await execute_ai_review_task(db, message.payload)

    elif message.event_type == OutboxEventType.FEEDBACK_REQUEST_SENT:
        await execute_feedback_request_task(db, message.payload)

    else:
        logger.error(
            "unknown_outbox_event_type",
            message_id=message.id,
            event_type=message.event_type.value,
        )
        raise ValueError(f"Unknown event type: {message.event_type}")

    logger.info("outbox_message_dispatched", message_id=message.id)
