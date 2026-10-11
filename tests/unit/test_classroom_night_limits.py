"""Per-night budget: the normal cap and window, or none on an "uncapped" weekday."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from submissions_checker.core.config import Settings
from submissions_checker.workers.scheduled.classroom_nightly import UNCAPPED, night_limits


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        secret_key="x" * 32,
        database_url="postgresql+asyncpg://u:p@h/db",
        **kw,
    )


# 2026-10-12 00:00Z is 03:00 Monday in Kyiv; 2026-10-13 00:00Z is 03:00 Tuesday.
MONDAY_RUN = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
TUESDAY_RUN = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


def test_normal_night_uses_cap_and_window() -> None:
    s = _settings(llm_grading_uncapped_weekdays="0")
    deadline, cap = night_limits(s, TUESDAY_RUN)
    assert cap == 40 and deadline == TUESDAY_RUN + timedelta(hours=1)


def test_uncapped_weekday_lifts_cap_and_extends_window() -> None:
    s = _settings(llm_grading_uncapped_weekdays="0", llm_grading_uncapped_end_hour=7)
    deadline, cap = night_limits(s, MONDAY_RUN)
    assert cap == UNCAPPED and deadline == MONDAY_RUN + timedelta(hours=4)


def test_weekday_is_taken_in_the_grading_timezone() -> None:
    # 23:30Z Sunday is already 02:30 Monday in Kyiv
    late_sunday_utc = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)
    _, cap = night_limits(_settings(llm_grading_uncapped_weekdays="0"), late_sunday_utc)
    assert cap == UNCAPPED


def test_no_uncapped_weekdays_by_default() -> None:
    _, cap = night_limits(_settings(), MONDAY_RUN)
    assert cap == 40
