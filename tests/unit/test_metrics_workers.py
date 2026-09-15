"""Worker-side counters: email dispatch outcomes.

Check, AI-review and outbox outcomes are asserted inside the tests that already
arrange those flows (test_check_task_delegation, test_ai_review_flow, test_workers).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from submissions_checker.core import metrics
from submissions_checker.services.notifications.dispatcher import NotificationDispatcher


def _val(outcome: str) -> float:
    return metrics.notifications_sent_total.labels(outcome=outcome)._value.get()


@pytest.mark.asyncio
async def test_dispatcher_counts_sent_and_failed() -> None:
    ok = MagicMock()
    ok.send = AsyncMock()
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("smtp down"))
    sent, failed = _val("sent"), _val("failed")

    await NotificationDispatcher([ok]).notify("a@b.c", "s", "b")
    with pytest.raises(RuntimeError):
        await NotificationDispatcher([bad]).notify("a@b.c", "s", "b")

    assert _val("sent") == sent + 1
    assert _val("failed") == failed + 1
