"""Anti-cheat event timeline helpers.

The browser is untrusted: what it says about an event is whitelisted, type-checked and
clamped here before it is stored or logged. A malformed context is dropped, never refused —
the event itself must still be recorded.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

# Sent by the client when the student comes back; they carry how long they were away. They
# are evidence, not violations: never counted, never matched against a rule.
INFORMATIONAL_EVENT_TYPES = frozenset({"tab_return", "focus_return"})
MAX_EVENTS_STORED_PER_ATTEMPT = 500

_MAX_CTX_BYTES = 1024
_INT_MAX = 10**9
_INT_KEYS = frozenset(
    {"vw", "vh", "ms_since_load", "from_w", "from_h", "to_w", "to_h", "away_ms", "faces", "held_ms"}
)
_ANGLE_KEYS = frozenset({"yaw", "pitch"})
_VISIBILITY = frozenset({"visible", "hidden"})
_COMBO_RE = re.compile(r"((ctrl|alt|meta|shift)\+){0,4}[a-z0-9]{1,12}")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def sanitize_ctx(raw: object) -> dict[str, Any]:
    """Return only the known, well-typed, bounded keys of a client event context."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _INT_KEYS:
            number = _number(value)
            if number is not None:
                clean[key] = min(max(int(number), 0), _INT_MAX)
        elif key in _ANGLE_KEYS:
            number = _number(value)
            if number is not None:
                clean[key] = round(min(max(number, -180.0), 180.0), 1)
        elif key == "has_focus" and isinstance(value, bool):
            clean[key] = value
        elif key == "visibility" and isinstance(value, str) and value in _VISIBILITY:
            clean[key] = value
        elif key == "combo" and isinstance(value, str) and _COMBO_RE.fullmatch(value):
            clean[key] = value
    if len(json.dumps(clean)) > _MAX_CTX_BYTES:
        return {}
    return clean


def offset_label(at: datetime, started_at: datetime) -> str:
    """``+MM:SS`` since the attempt started (minutes are not wrapped into hours)."""
    seconds = max(0, int((at - started_at).total_seconds()))
    minutes, sec = divmod(seconds, 60)
    return f"+{minutes:02d}:{sec:02d}"
