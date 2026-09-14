"""Background task scheduler using APScheduler."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized. Call init_scheduler() first.")
    return _scheduler


def init_scheduler() -> AsyncIOScheduler:
    """Initialize and configure the scheduler with all jobs."""
    global _scheduler

    if _scheduler is not None:
        logger.warning("Scheduler already initialized")
        return _scheduler

    logger.info("Initializing scheduler")
    _scheduler = AsyncIOScheduler()

    # Register scheduled jobs
    _register_jobs()

    return _scheduler


def _register_jobs() -> None:
    """Register all scheduled jobs with the scheduler."""
    from submissions_checker.core.config import get_settings
    from submissions_checker.workers.scheduled.outbox_processor import process_outbox_messages
    from submissions_checker.workers.scheduled.teacher_digest_processor import (
        flush_teacher_digests,
    )

    settings = get_settings()
    scheduler = get_scheduler()

    # Outbox processor - runs every 10 seconds
    scheduler.add_job(
        process_outbox_messages,
        trigger=IntervalTrigger(seconds=10),
        id="outbox_processor",
        name="Process Outbox Messages",
        replace_existing=True,
        max_instances=1,  # Prevent concurrent runs
    )

    logger.info("Registered outbox processor job (interval: 10s)")

    # Teacher digest flusher - coalesces review-queue emails per teacher
    flush_interval = settings.teacher_digest_flush_interval
    scheduler.add_job(
        flush_teacher_digests,
        trigger=IntervalTrigger(seconds=flush_interval),
        id="teacher_digest_processor",
        name="Flush Teacher Review Digests",
        replace_existing=True,
        max_instances=1,
    )

    logger.info("Registered teacher digest job (interval: %ss)", flush_interval)


async def start_scheduler() -> None:
    """Start the scheduler."""
    scheduler = get_scheduler()

    if scheduler.running:
        logger.warning("Scheduler already running")
        return

    scheduler.start()
    logger.info("Scheduler started")


async def shutdown_scheduler() -> None:
    """Shutdown the scheduler gracefully."""
    global _scheduler

    if _scheduler is None:
        logger.warning("Scheduler not initialized")
        return

    if not _scheduler.running:
        logger.warning("Scheduler not running")
        return

    logger.info("Shutting down scheduler")
    _scheduler.shutdown(wait=True)
    logger.info("Scheduler shutdown complete")
