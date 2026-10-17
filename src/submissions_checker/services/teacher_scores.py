"""Legacy ``review_mode: quiz_and_teacher_scores`` config: detection, criteria, validation.

Grading itself lives in ``services.requirements`` / ``services.settle``; this module only
reads and validates the old config shape, which ``requirements.from_config`` translates.
Pure and DB-free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

MODE = "quiz_and_teacher_scores"
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    max: int
    optional: bool


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
