"""Scheduled jobs run with `job` bound and a clean context."""

from __future__ import annotations

import pytest
import structlog
from structlog.testing import capture_logs

from submissions_checker.core.logging import get_logger
from submissions_checker.core.scheduler import with_job_context

pytestmark = pytest.mark.asyncio


async def test_job_context_is_bound_and_cleared() -> None:
    log = get_logger("t.job")
    structlog.contextvars.bind_contextvars(request_id="stale")

    async def job() -> None:
        log.info("job_line")

    with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
        await with_job_context("outbox_processor", job)()

    (line,) = logs
    assert line["job"] == "outbox_processor"
    assert "request_id" not in line
    assert "job" not in structlog.contextvars.get_contextvars()
