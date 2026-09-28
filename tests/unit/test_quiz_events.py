"""Client-context whitelist and timeline offsets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from submissions_checker.services.quiz_events import (
    INFORMATIONAL_EVENT_TYPES,
    MAX_EVENTS_STORED_PER_ATTEMPT,
    offset_label,
    sanitize_ctx,
)


def test_keeps_whitelisted_values() -> None:
    raw = {
        "visibility": "hidden",
        "has_focus": False,
        "vw": 1920,
        "vh": 1080,
        "ms_since_load": 5000,
        "away_ms": 42000,
        "from_w": 1920,
        "from_h": 1080,
        "to_w": 1920,
        "to_h": 640,
        "faces": 0,
        "held_ms": 3000,
        "yaw": -31.26,
        "pitch": 12,
        "combo": "ctrl+shift+i",
    }
    clean = sanitize_ctx(raw)
    assert clean == {**raw, "yaw": -31.3, "pitch": 12.0}


@pytest.mark.parametrize("raw", [None, "junk", 5, [1, 2], {"vw": "abc"}, {"evil": 1}])
def test_garbage_becomes_empty(raw: object) -> None:
    assert sanitize_ctx(raw) == {}


def test_drops_invalid_values_but_keeps_valid_neighbours() -> None:
    raw = {
        "vw": float("nan"),
        "vh": float("inf"),
        "has_focus": "yes",
        "visibility": ["hidden"],
        "combo": "ctrl+<script>",
        "faces": True,
        "away_ms": 1200,
        "nested": {"a": 1},
    }
    assert sanitize_ctx(raw) == {"away_ms": 1200}


def test_clamps_numbers() -> None:
    clean = sanitize_ctx({"away_ms": -5, "vw": 10**12, "yaw": 999, "pitch": -999})
    assert clean == {"away_ms": 0, "vw": 10**9, "yaw": 180.0, "pitch": -180.0}


@pytest.mark.parametrize("combo", ["ctrl+c", "f12", "printscreen", "ctrl+alt+meta+shift+i"])
def test_accepts_shortcut_combos(combo: str) -> None:
    assert sanitize_ctx({"combo": combo}) == {"combo": combo}


@pytest.mark.parametrize("combo", ["hello world", "ctrl+", "CTRL+C", "a" * 13, "super+c"])
def test_rejects_anything_that_could_be_typed_text(combo: str) -> None:
    assert sanitize_ctx({"combo": combo}) == {}


def test_constants() -> None:
    assert INFORMATIONAL_EVENT_TYPES == frozenset({"tab_return", "focus_return"})
    assert MAX_EVENTS_STORED_PER_ATTEMPT == 500


def test_offset_label() -> None:
    start = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
    assert offset_label(start + timedelta(minutes=4, seconds=12), start) == "+04:12"
    assert offset_label(start + timedelta(minutes=75, seconds=3), start) == "+75:03"
    assert offset_label(start - timedelta(seconds=3), start) == "+00:00"
