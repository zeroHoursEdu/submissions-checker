"""What must be done for an assignment's work to count, and what it is worth.

An assignment declares ``requirements:`` — independent items (quiz, teacher score, teacher
check, sandbox tests, AI review) done in any order. The grade appears once every required
item is done and is ``min_grade`` plus the items' points. Pure and DB-free: config_apply,
settle, the board and the student page share it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

ItemType = Literal["quiz", "score", "check", "tests", "ai_review"]
ITEM_TYPES: frozenset[str] = frozenset({"quiz", "score", "check", "tests", "ai_review"})
CODE_TYPES: frozenset[str] = frozenset({"tests", "ai_review"})
TEACHER_TYPES: frozenset[str] = frozenset({"score", "check"})
SOURCES: frozenset[str] = frozenset({"upload", "classroom"})
_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_DEFAULT_TITLES = {"quiz": "Тест", "tests": "Тести", "ai_review": "AI-рев'ю"}


class RequirementsError(ValueError):
    """An assignment's requirements cannot be used as written."""


@dataclass(frozen=True)
class Requirement:
    key: str
    type: ItemType
    title: str
    points: float
    optional: bool = False
    llm: str | None = None


@dataclass(frozen=True)
class Plan:
    items: tuple[Requirement, ...]
    source: Literal["upload", "classroom"] | None
    rounding: Literal["half_up", "bankers"] = "half_up"

    def has(self, item_type: str) -> bool:
        return any(i.type == item_type for i in self.items)

    def of(self, item_type: str) -> list[Requirement]:
        return [i for i in self.items if i.type == item_type]

    def teacher_items(self) -> list[Requirement]:
        return [i for i in self.items if i.type in TEACHER_TYPES]

    def llm_items(self) -> list[Requirement]:
        return [i for i in self.items if i.type == "score" and i.llm]


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _item(where: str, raw: Any) -> Requirement:
    if not isinstance(raw, Mapping):
        raise RequirementsError(f"{where}: each requirement must be a mapping")
    item_type = raw.get("type")
    if item_type not in ITEM_TYPES:
        raise RequirementsError(f"{where}: type {item_type!r} must be one of {sorted(ITEM_TYPES)}")
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise RequirementsError(f"{where}: key {key!r} must match [a-z0-9_]+")
    if item_type in TEACHER_TYPES and not str(raw.get("title") or "").strip():
        raise RequirementsError(f"{where}: {item_type} '{key}' needs a title")
    if item_type == "score":
        amount = raw.get("max")
        if not _is_int(amount) or amount < 0:
            raise RequirementsError(f"{where}: score '{key}' max must be a non-negative integer")
    else:
        if "max" in raw:
            raise RequirementsError(f"{where}: {item_type} '{key}' takes points, not max")
        amount = raw.get("points", 0)
        if not _is_int(amount) or amount < 0:
            raise RequirementsError(f"{where}: {item_type} '{key}' points must be >= 0")
    llm = raw.get("llm")
    if llm is not None and item_type != "score":
        raise RequirementsError(f"{where}: only score items take llm (got it on '{key}')")
    if llm is not None and not (isinstance(llm, str) and llm.strip()):
        raise RequirementsError(f"{where}: score '{key}' llm must be non-empty rubric text")
    return Requirement(
        key=key,
        type=item_type,
        title=str(raw.get("title") or _DEFAULT_TITLES.get(item_type, key)),
        points=amount,
        optional=bool(raw.get("optional", False)),
        llm=llm.strip() if isinstance(llm, str) else None,
    )


def parse(a_cfg: Mapping[str, Any]) -> Plan:
    """The plan of a new-style assignment (one with ``requirements:``)."""
    raw = a_cfg.get("requirements")
    if not isinstance(raw, list) or not raw:
        raise RequirementsError("requirements must be a non-empty list")
    items = tuple(_item("requirements", r) for r in raw)
    source = a_cfg.get("source")
    if source is None and any(i.type in CODE_TYPES for i in items):
        source = "upload"
    if source is not None and source not in SOURCES:
        raise RequirementsError(f"source {source!r} must be one of {sorted(SOURCES)}")
    return Plan(items=items, source=source)


def validate(code: str, a_cfg: Mapping[str, Any], *, has_quiz_block: bool) -> None:
    """Raise RequirementsError unless a new-style assignment can be graded as written."""
    where = f"assignment '{code}'"
    if "review_mode" in a_cfg:
        raise RequirementsError(f"{where}: drop review_mode; requirements replace it")
    try:
        plan = parse(a_cfg)
    except RequirementsError as exc:
        raise RequirementsError(f"{where}: {exc}") from exc
    seen: set[str] = set()
    for item in plan.items:
        if item.key in seen:
            raise RequirementsError(f"{where}: duplicate requirement key '{item.key}'")
        seen.add(item.key)
    if plan.has("quiz") and not has_quiz_block:
        raise RequirementsError(f"{where}: a quiz item needs a quiz block with questions")
    if has_quiz_block and not plan.has("quiz"):
        raise RequirementsError(f"{where}: has a quiz block but no quiz item")
    if len(plan.of("quiz")) > 1:
        raise RequirementsError(f"{where}: at most one quiz item")
    if plan.source == "classroom" and any(i.type in CODE_TYPES for i in plan.items):
        raise RequirementsError(
            f"{where}: tests/ai_review on Classroom works are not supported yet"
        )
    total = sum(i.points for i in plan.items)
    span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    if total != span:
        raise RequirementsError(
            f"{where}: requirement points ({total:g}) must equal max_grade - min_grade ({span})"
        )
