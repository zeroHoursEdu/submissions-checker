"""Unit tests for utils.datetime helpers — tz awareness and round-trips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from submissions_checker.utils import datetime as dtutil


def test_utcnow_is_timezone_aware_utc() -> None:
    now = dtutil.utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_assumes_naive_is_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    out = dtutil.to_utc(naive)
    assert out.tzinfo is timezone.utc
    # wall-clock unchanged, just tagged as UTC
    assert out.replace(tzinfo=None) == naive


def test_to_utc_converts_other_timezone() -> None:
    plus5 = timezone(timedelta(hours=5))
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=plus5)
    out = dtutil.to_utc(aware)
    assert out.utcoffset() == timedelta(0)
    # 12:00 at +5 == 07:00 UTC
    assert out.hour == 7
    assert out.day == 1


def test_to_utc_idempotent_on_utc_value() -> None:
    aware = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
    assert dtutil.to_utc(aware) == aware


def test_format_iso_round_trips_with_parse() -> None:
    dt = datetime(2026, 3, 4, 5, 6, 7, 890123, tzinfo=UTC)
    s = dtutil.format_iso(dt)
    assert dtutil.parse_iso(s) == dt


def test_parse_iso_naive_string() -> None:
    parsed = dtutil.parse_iso("2026-12-31T23:59:59")
    assert parsed == datetime(2026, 12, 31, 23, 59, 59)
    assert parsed.tzinfo is None


def test_parse_iso_with_offset() -> None:
    parsed = dtutil.parse_iso("2026-01-01T00:00:00+02:00")
    assert parsed.utcoffset() == timedelta(hours=2)


def test_format_then_to_utc_chain() -> None:
    plus2 = timezone(timedelta(hours=2))
    dt = datetime(2026, 1, 1, 1, 0, 0, tzinfo=plus2)
    utc = dtutil.to_utc(dtutil.parse_iso(dtutil.format_iso(dt)))
    assert utc.hour == 23
    assert utc.day == 31  # rolled back to prev year-end
    assert utc.year == 2025
