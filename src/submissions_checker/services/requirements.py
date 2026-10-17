"""What must be done for an assignment's work to count, and what it is worth.

An assignment declares ``requirements:`` — independent items (quiz, teacher score, teacher
check, sandbox tests, AI review) done in any order. The grade appears once every required
item is done and is ``min_grade`` plus the items' points. Pure and DB-free: config_apply,
settle, the board and the student page share it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
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


_ACCEPTED = Requirement(key="accepted", type="check", title="Прийнято викладачем", points=0)
_LEGACY_ITEMS: dict[str, tuple[str, ...]] = {
    "tests_only": ("tests",),
    "tests_then_ai": ("tests", "ai_review"),
    "tests_then_teacher": ("tests", "check"),
    "tests_then_ai_then_teacher": ("tests", "ai_review", "check"),
    "tests_then_ai_teacher": ("tests", "ai_review", "check"),
    "tests_then_quiz": ("tests", "quiz"),
    "tests_then_ai_then_quiz": ("tests", "ai_review", "quiz"),
    "quiz_only": ("quiz",),
    "quiz_then_teacher": ("quiz", "check"),
}


def _translate_scored(a_cfg: Mapping[str, Any]) -> Plan:
    from submissions_checker.services import teacher_scores  # legacy reader, no cycle

    grading = a_cfg.get("grading") or {}
    raw_by_key = {r.get("key"): r for r in grading.get("teacher_criteria") or []}
    items = [Requirement("quiz", "quiz", "Тест", int(grading.get("quiz_points", 0)))]
    for c in teacher_scores.criteria(grading):
        raw = raw_by_key.get(c.key) or {}
        llm = (
            None
            if raw.get("llm") is False
            else (str(raw.get("requirements") or "").strip() or None)
        )
        items.append(Requirement(c.key, "score", c.title, c.max, c.optional, llm))
    llm_on = (a_cfg.get("llm_grading") or {}).get("enabled") is True
    return Plan(items=tuple(items), source="classroom" if llm_on else None)


def _translate_weighted(a_cfg: Mapping[str, Any], mode: str, span: int | None) -> Plan:
    grading = a_cfg.get("grading") or {}
    code = grading.get("code") or {}
    code_w = float(grading.get("code_weight", 1.0))
    kinds = _LEGACY_ITEMS.get(mode, _LEGACY_ITEMS["tests_only"])
    # The old blend first mixes works and quality into one code score (weights works_weight /
    # quality_weight, absent parts dropped), then weighs that score against the quiz by
    # code_weight. So the code group's weight is code_weight however it splits inside.
    inner = {
        "tests": max(float(code.get("works_weight", 1.0)), 0.0),
        "ai_review": max(float(code.get("quality_weight", 0.0)), 0.0),
    }
    inner_total = sum(inner[k] for k in kinds if k in inner)
    weights = {k: code_w * v / inner_total if inner_total > 0 else 0.0 for k, v in inner.items()}
    weights["quiz"] = float(grading.get("quiz_weight", 0.0))
    total_w = sum(max(weights[k], 0.0) for k in kinds if k != "check")
    if span is None:
        span = int(a_cfg.get("max_grade", 100)) - int(a_cfg.get("min_grade", 0))
    items: list[Requirement] = []
    for k in kinds:
        if k == "check":
            items.append(_ACCEPTED)
            continue
        share = max(weights[k], 0.0) / total_w if total_w > 0 else 0.0
        items.append(Requirement(k, k, _DEFAULT_TITLES[k], span * share))  # type: ignore[arg-type]
    return Plan(items=tuple(items), source="upload", rounding="bankers")


def from_config(a_cfg: Mapping[str, Any] | None, *, span: int | None = None) -> Plan:
    """The assignment's plan: its ``requirements`` list, or its old review mode translated."""
    cfg = a_cfg or {}
    if "requirements" in cfg:
        return parse(cfg)
    mode = str(cfg.get("review_mode") or "tests_only")
    if mode == "quiz_and_teacher_scores":
        return _translate_scored(cfg)
    return _translate_weighted(cfg, mode, span)


State = Literal["done", "pending", "running", "failed"]


@dataclass(frozen=True)
class Facts:
    quiz: tuple[State, float | None] = ("pending", None)
    tests: tuple[State, float | None] = ("pending", None)
    ai_review: tuple[State, float | None] = ("pending", None)
    scores: Mapping[str, int] = field(default_factory=dict)
    checks_done: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ItemResult:
    key: str
    type: str
    title: str
    state: State
    points: float
    max: float
    optional: bool


@dataclass(frozen=True)
class Evaluation:
    items: tuple[ItemResult, ...]
    complete: bool
    failed: bool
    grade: int | None
    breakdown: dict[str, Any] | None

    def pending_teacher(self) -> bool:
        return any(
            i.type in TEACHER_TYPES and not i.optional and i.state != "done" for i in self.items
        )

    def first_pending(self) -> ItemResult | None:
        return next((i for i in self.items if not i.optional and i.state != "done"), None)


def _result(item: Requirement, facts: Facts) -> ItemResult:
    state: State
    earned = 0.0
    if item.type in ("quiz", "tests", "ai_review"):
        state, pct = getattr(facts, item.type)
        if state == "done":
            earned = item.points * float(pct or 0.0) / 100.0
    elif item.type == "score":
        value = facts.scores.get(item.key)
        state = "done" if value is not None else "pending"
        earned = float(value or 0)
    else:  # check
        ticked = facts.scores.get(item.key) == 1 or item.key in facts.checks_done
        state = "done" if ticked else "pending"
        earned = float(item.points) if ticked else 0.0
    return ItemResult(item.key, item.type, item.title, state, earned, item.points, item.optional)


def _round(total: float, rounding: str, round_up: bool) -> int:
    total = round(total, 6)
    if round_up:
        return math.ceil(total)
    if rounding == "bankers":
        return round(total)
    return int(Decimal(str(total)).quantize(Decimal(1), ROUND_HALF_UP))


def evaluate(
    plan: Plan, facts: Facts, min_grade: int, max_grade: int, *, round_up: bool
) -> Evaluation:
    """Each item's state and points; a grade only once every required item is done."""
    items = tuple(_result(i, facts) for i in plan.items)
    required = [i for i in items if not i.optional]
    failed = any(i.state == "failed" for i in required)
    complete = not failed and all(i.state == "done" for i in required)
    if not complete:
        return Evaluation(items, False, failed, None, None)
    earned = sum(i.points for i in items if i.state == "done")
    grade = max(min_grade, min(max_grade, _round(min_grade + earned, plan.rounding, round_up)))
    breakdown = {
        "grade": grade,
        "mode": "requirements",
        "quiz_score": facts.quiz[1] if facts.quiz[0] == "done" else None,
        "works_score": facts.tests[1] if facts.tests[0] == "done" else None,
        "quality_score": facts.ai_review[1] if facts.ai_review[0] == "done" else None,
        "items": [
            {
                "key": i.key,
                "type": i.type,
                "title": i.title,
                "state": i.state,
                "points": round(i.points, 2),
                "max": i.max,
            }
            for i in items
        ],
    }
    return Evaluation(items, True, False, grade, breakdown)
