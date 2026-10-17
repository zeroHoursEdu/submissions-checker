"""Quiz-percentage helpers shared by settle and the squad flows."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _pct(score: float | int | None, max_score: float | int | None) -> float | None:
    if not max_score or max_score <= 0 or score is None:
        return None
    return (score / max_score) * 100.0


def squad_quiz_pct(attempts: Iterable[Any]) -> tuple[float | None, list[dict[str, Any]]]:
    """Mean quiz percentage over squad members, one passed attempt per ``student_id``.

    Returns ``(mean_pct, members)`` where ``members`` is the per-member list stored in
    ``grade_breakdown["squad"]["members"]``. ``(None, [])`` when nobody has passed yet.
    """
    best: dict[int, float] = {}
    for a in attempts:
        if not a.is_passed or a.student_id is None:
            continue
        pct = _pct(a.score, a.max_score)
        if pct is not None and pct > best.get(a.student_id, -1.0):
            best[a.student_id] = pct
    if not best:
        return None, []
    members = [{"student_id": sid, "quiz_pct": round(p, 2)} for sid, p in sorted(best.items())]
    return sum(best.values()) / len(best), members
