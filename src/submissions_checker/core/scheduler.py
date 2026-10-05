"""Background task scheduler using APScheduler."""

import functools
import logging
from collections.abc import Awaitable, Callable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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


def with_job_context(
    job_id: str, func: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    """Run a scheduled job with only `job` bound — nothing from another job or request."""

    @functools.wraps(func)
    async def run() -> None:
        structlog.contextvars.clear_contextvars()
        try:
            with structlog.contextvars.bound_contextvars(job=job_id):
                await func()
        finally:
            structlog.contextvars.clear_contextvars()

    return run


def _register_jobs() -> None:
    """Register all scheduled jobs with the scheduler."""
    from submissions_checker.core.config import get_settings
    from submissions_checker.workers.scheduled.classroom_nightly import run_classroom_nightly
    from submissions_checker.workers.scheduled.deadline_reminders import run_deadline_reminders
    from submissions_checker.workers.scheduled.metrics_refresh import refresh_metrics
    from submissions_checker.workers.scheduled.outbox_processor import process_outbox_messages
    from submissions_checker.workers.scheduled.subject_stats_refresh import (
        refresh_subject_gradebook_stats,
    )
    from submissions_checker.workers.scheduled.teacher_digest_processor import (
        flush_teacher_digests,
    )

    settings = get_settings()
    scheduler = get_scheduler()

    # Outbox processor - runs every 10 seconds
    scheduler.add_job(
        with_job_context("outbox_processor", process_outbox_messages),
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
        with_job_context("teacher_digest_processor", flush_teacher_digests),
        trigger=IntervalTrigger(seconds=flush_interval),
        id="teacher_digest_processor",
        name="Flush Teacher Review Digests",
        replace_existing=True,
        max_instances=1,
    )

    logger.info("Registered teacher digest job (interval: %ss)", flush_interval)

    # Metrics refresh - DB-derived gauges for the Grafana dashboards
    scheduler.add_job(
        with_job_context("metrics_refresh", refresh_metrics),
        trigger=IntervalTrigger(seconds=settings.metrics_refresh_interval),
        id="metrics_refresh",
        name="Refresh Prometheus gauges",
        replace_existing=True,
        max_instances=1,
    )

    logger.info("Registered metrics refresh job (interval: %ss)", settings.metrics_refresh_interval)

    # Subject gradebook stats — cached Панель stat-card numbers
    scheduler.add_job(
        with_job_context("subject_stats_refresh", refresh_subject_gradebook_stats),
        trigger=IntervalTrigger(seconds=settings.subject_stats_refresh_interval),
        id="subject_stats_refresh",
        name="Refresh subject gradebook stats",
        replace_existing=True,
        max_instances=1,
    )

    logger.info(
        "Registered subject stats refresh job (interval: %ss)",
        settings.subject_stats_refresh_interval,
    )

    # Deadline reminders — enqueue DEADLINE_REMINDER emails for unsubmitted work.
    # Opt-in (DEADLINE_REMINDERS_ENABLED=true): see the setting's comment.
    if settings.deadline_reminders_enabled:
        scheduler.add_job(
            with_job_context("deadline_reminders", run_deadline_reminders),
            trigger=IntervalTrigger(seconds=settings.deadline_reminder_interval),
            id="deadline_reminders",
            name="Enqueue deadline reminders",
            replace_existing=True,
            max_instances=1,
        )
        logger.info(
            "Registered deadline reminders job (interval: %ss)",
            settings.deadline_reminder_interval,
        )
    else:
        logger.info("deadline_reminders_disabled")

    # Classroom ingest + LLM grading — nightly, only when Google is configured.
    if settings.classroom_enabled:
        scheduler.add_job(
            with_job_context("classroom_nightly", run_classroom_nightly),
            trigger=CronTrigger(
                hour=settings.llm_grading_start_hour,
                minute=0,
                timezone=settings.llm_grading_timezone,
            ),
            id="classroom_nightly",
            name="Classroom ingest + LLM grading",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=1800,
            coalesce=True,
        )
        logger.info(
            "Registered classroom nightly job (%02d:00 %s)",
            settings.llm_grading_start_hour,
            settings.llm_grading_timezone,
        )


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
