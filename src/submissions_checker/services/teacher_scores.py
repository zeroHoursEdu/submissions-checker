"""Grade rules for ``review_mode: quiz_and_teacher_scores``.

The platform examines only the defence (a quiz); the teacher grades the work outside it and
types per-criterion points on the assignment board. The grade is quiz points plus teacher
points. Pure and DB-free: config validation, form parsing and the formula live here so
``config_apply``, ``grading`` and the teacher route share one definition.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeGuard

MODE = "quiz_and_teacher_scores"
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    max: int
    optional: bool


class ScoreError(ValueError):
    """Teacher input that cannot be stored as points."""


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def is_scored_mode(assignment_config: Mapping[str, Any] | None) -> bool:
    return (assignment_config or {}).get("review_mode") == MODE


def criteria(grading_cfg: Mapping[str, Any] | None) -> list[Criterion]:
    """The configured criteria, in config order (assumes ``validate_assignment`` passed)."""
    return [
        Criterion(
            key=str(raw["key"]),
            title=str(raw.get("title") or raw["key"]),
            max=int(raw["max"]),
            optional=bool(raw.get("optional", False)),
        )
        for raw in (grading_cfg or {}).get("teacher_criteria") or []
    ]


def validate_assignment(code: str, a_cfg: Mapping[str, Any]) -> None:
    """Raise ValueError unless the assignment's points can add up to its grade range."""
    where = f"assignment '{code}' ({MODE})"
    if not (a_cfg.get("quiz") or {}).get("questions"):
        raise ValueError(f"{where} needs a quiz with questions")
    grading = a_cfg.get("grading") or {}
    quiz_points = grading.get("quiz_points")
    if not _is_int(quiz_points) or quiz_points < 0:
        raise ValueError(f"{where}: grading.quiz_points must be a non-negative integer")
    raw = grading.get("teacher_criteria")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where}: grading.teacher_criteria must list at least one criterion")
    seen: set[str] = set()
    total = 0
    for item in raw:
        key = (item or {}).get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError(f"{where}: criterion key {key!r} must match [a-z0-9_]+")
        if key in seen:
            raise ValueError(f"{where}: duplicate criterion key '{key}'")
        seen.add(key)
        mx = item.get("max")
        if not _is_int(mx) or mx <= 0:
            raise ValueError(f"{where}: criterion '{key}' max must be a positive integer")
        total += mx
    span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    if quiz_points + total != span:
        raise ValueError(
            f"{where}: quiz_points + criteria max ({quiz_points} + {total}) must equal "
            f"max_grade - min_grade ({span})"
        )


def parse_form(crits: list[Criterion], form: Mapping[str, str]) -> dict[str, int]:
    """Read ``score_<key>`` fields; an empty field means "not entered" and is left out."""
    out: dict[str, int] = {}
    for c in crits:
        raw = (form.get(f"score_{c.key}") or "").strip()
        if raw == "":
            continue
        if not raw.isdigit():
            raise ScoreError(f"{c.title}: '{raw}' is not a whole number")
        value = int(raw)
        if value > c.max:
            raise ScoreError(f"{c.title}: {value} is above the maximum {c.max}")
        out[c.key] = value
    return out


def is_complete(crits: list[Criterion], scores: Mapping[str, int] | None) -> bool:
    """True when every required criterion has a value (optional ones may stay empty)."""
    have = scores or {}
    return all(c.optional or c.key in have for c in crits)


def quiz_points_for(
    grading_cfg: Mapping[str, Any] | None, quiz_pct: float, *, round_up: bool = False
) -> int:
    """The quiz half in points: ``quiz_pct`` of ``quiz_points``, half up (squads: ceil)."""
    raw = round(quiz_pct / 100.0 * int((grading_cfg or {}).get("quiz_points", 0)), 6)
    if round_up:
        return math.ceil(raw)
    return int(Decimal(str(raw)).quantize(Decimal(1), ROUND_HALF_UP))


def compute(
    grading_cfg: Mapping[str, Any] | None,
    min_grade: int,
    max_grade: int,
    *,
    quiz_pct: float,
    scores: Mapping[str, int],
    round_up: bool = False,
) -> dict[str, Any]:
    """The ``grade_breakdown`` dict: quiz share of ``quiz_points`` plus every criterion.

    Rounds the quiz half up (a teacher reads 4.5 as 5, not banker's 4); squads round up,
    as the blended path does for them.
    """
    cfg = grading_cfg or {}
    quiz_max = int(cfg.get("quiz_points", 0))
    quiz_awarded = quiz_points_for(cfg, quiz_pct, round_up=round_up)
    crits = criteria(cfg)
    points = [int(scores.get(c.key, 0)) for c in crits]
    rows = [
        {"key": c.key, "title": c.title, "points": p, "max": c.max}
        for c, p in zip(crits, points, strict=True)
    ]
    total = min_grade + quiz_awarded + sum(points)
    return {
        "grade": max(min_grade, min(max_grade, total)),
        "mode": MODE,
        "quiz_score": quiz_pct,
        "quiz": {"pct": quiz_pct, "points": quiz_awarded, "max": quiz_max},
        "criteria": rows,
    }
