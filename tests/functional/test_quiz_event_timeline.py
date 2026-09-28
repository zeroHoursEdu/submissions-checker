"""The anti-cheat timeline: every /event call leaves a row explaining what happened."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from structlog.testing import capture_logs

from submissions_checker.db.models import QuizAttemptEvent
from submissions_checker.db.models.enums import QuizAttemptStatus, QuizEventOutcome
from tests.functional.test_student_quiz_proctoring import (
    _arrange_quiz,
    _consent,
    _make_attempt,
    _rule,
)

pytestmark = pytest.mark.asyncio


async def _events(db, attempt_id: int) -> list[QuizAttemptEvent]:
    db.expire_all()
    rows = await db.execute(
        select(QuizAttemptEvent)
        .where(QuizAttemptEvent.attempt_id == attempt_id)
        .order_by(QuizAttemptEvent.id)
    )
    return list(rows.scalars())


async def _attempt(db, student_user, rules: list[dict] | None = None, **kwargs):
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    return await _make_attempt(
        db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": rules or []}}, **kwargs
    )


async def test_each_event_is_recorded_with_its_rule_and_context(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(
        db, student_user, [_rule("tab_switch", 2, {"type": "warn", "message": "m"})]
    )
    ctx = {"visibility": "hidden", "has_focus": False, "vw": 1280, "vh": 720, "evil": "x"}
    for _ in range(2):
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/event",
            json={"type": "tab_switch", "ctx": ctx},
            headers={"User-Agent": "TestBrowser/1.0", "X-Forwarded-For": "203.0.113.9"},
        )
        assert r.status_code == 200

    first, second = await _events(db, attempt.id)
    assert (first.count_after, first.action, first.rule_threshold) == (1, "none", None)
    assert (second.count_after, second.action, second.rule_threshold) == (2, "warn", 2)
    assert first.outcome == second.outcome == QuizEventOutcome.APPLIED
    assert second.client_ctx == {"visibility": "hidden", "has_focus": False, "vw": 1280, "vh": 720}
    assert second.user_agent == "TestBrowser/1.0"
    assert second.ip == "203.0.113.9"


async def test_fail_rule_is_recorded_and_logged(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(
        db, student_user, [_rule("paste", 1, {"type": "fail", "message": "x"})]
    )
    attempt_id = attempt.id
    student_id = student_user.student_id
    with capture_logs() as logs:
        r = await student_client.post(f"/portal/quiz/{attempt_id}/event", json={"type": "paste"})
    assert r.json()["action"] == "fail"

    (row,) = await _events(db, attempt_id)
    assert (row.action, row.rule_threshold, row.count_after) == ("fail", 1, 1)
    failed = next(e for e in logs if e["event"] == "quiz_attempt_force_failed")
    assert failed["log_level"] == "warning"
    assert failed["attempt_id"] == attempt_id
    assert failed["student_id"] == student_id
    assert (failed["event_type"], failed["rule_threshold"], failed["count_after"]) == (
        "paste",
        1,
        1,
    )
    decision = next(e for e in logs if e["event"] == "quiz_anticheat_event")
    assert decision["action"] == "fail" and decision["log_level"] == "warning"


async def test_return_events_are_informational_and_never_counted(
    student_client: AsyncClient, db, student_user
) -> None:
    # Even a config that names tab_return in a fail rule must not fire on it.
    attempt = await _attempt(
        db, student_user, [_rule("tab_return", 1, {"type": "fail", "message": "x"})]
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_return", "ctx": {"away_ms": 42000}}
    )
    assert r.json()["action"] == "none"

    (row,) = await _events(db, attempt.id)
    assert row.outcome == QuizEventOutcome.INFORMATIONAL
    assert row.count_after is None
    assert row.client_ctx == {"away_ms": 42000}
    await db.refresh(attempt)
    assert attempt.violations == {}


async def test_events_during_an_air_raid_pause_are_recorded_as_ignored(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(
        db, student_user, [_rule("tab_switch", 1, {"type": "fail", "message": "x"})]
    )
    attempt.paused_at = datetime.now(UTC)
    await db.commit()

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    assert r.json() == {"action": "none", "violation_count": 0, "paused": True}

    (row,) = await _events(db, attempt.id)
    assert row.outcome == QuizEventOutcome.IGNORED_PAUSED
    await db.refresh(attempt)
    assert attempt.violations == {}


async def test_events_after_the_attempt_ended_are_recorded_as_ignored(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(db, student_user, status=QuizAttemptStatus.COMPLETED)
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    assert r.json() == {"action": "none", "violation_count": 0}
    (row,) = await _events(db, attempt.id)
    assert row.outcome == QuizEventOutcome.IGNORED_NOT_IN_PROGRESS


async def test_invented_event_types_past_the_cap_are_recorded_as_ignored(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(db, student_user, violations={f"e{i}": 1 for i in range(32)})
    await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "brand_new"})
    (row,) = await _events(db, attempt.id)
    assert row.outcome == QuizEventOutcome.IGNORED_TYPE_CAP
    await db.refresh(attempt)
    assert "brand_new" not in attempt.violations


async def test_rows_stop_at_the_cap_but_counting_goes_on(
    student_client: AsyncClient, db, student_user
) -> None:
    attempt = await _attempt(db, student_user)
    db.add_all(
        QuizAttemptEvent(
            attempt_id=attempt.id,
            event_type="tab_switch",
            action="none",
            outcome=QuizEventOutcome.APPLIED,
        )
        for _ in range(500)
    )
    await db.commit()

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    assert r.json()["violation_count"] == 1
    assert len(await _events(db, attempt.id)) == 500


async def test_the_row_explaining_an_auto_fail_is_stored_past_the_cap(
    student_client: AsyncClient, db, student_user
) -> None:
    # The client controls how many informational/ignored rows exist; it must not be able
    # to crowd out the one row a teacher needs to understand a force-fail.
    attempt = await _attempt(
        db, student_user, [_rule("paste", 1, {"type": "fail", "message": "x"})]
    )
    db.add_all(
        QuizAttemptEvent(
            attempt_id=attempt.id,
            event_type="tab_return",
            action="none",
            outcome=QuizEventOutcome.INFORMATIONAL,
        )
        for _ in range(500)
    )
    await db.commit()

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "paste"})
    assert r.json()["action"] == "fail"
    rows = await _events(db, attempt.id)
    assert len(rows) == 501
    assert (rows[-1].event_type, rows[-1].action) == ("paste", "fail")


@pytest.mark.parametrize("ctx", ["junk", 7, [1], {"vw": "abc"}, {"nested": {"a": 1}}])
async def test_malformed_context_never_blocks_the_event(
    student_client: AsyncClient, db, student_user, ctx: object
) -> None:
    attempt = await _attempt(db, student_user)
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch", "ctx": ctx}
    )
    assert r.status_code == 200
    (row,) = await _events(db, attempt.id)
    assert row.client_ctx == {}
