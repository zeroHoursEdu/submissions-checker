"""``llm_grading`` assignment config block: helpers and validation.

Pure and DB-free so ``config_apply`` (validation, task inlining) and the grading runner
share one definition of "this assignment is graded by the LLM".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from submissions_checker.services import teacher_scores

SOURCES = frozenset({"google_classroom"})


@dataclass(frozen=True)
class LLMCriterion:
    key: str
    title: str
    max: int
    requirements: str


def is_llm_graded(assignment_config: Mapping[str, Any] | None) -> bool:
    cfg = assignment_config or {}
    return (
        teacher_scores.is_scored_mode(cfg) and (cfg.get("llm_grading") or {}).get("enabled") is True
    )


def llm_criteria(grading_cfg: Mapping[str, Any] | None) -> list[LLMCriterion]:
    """Criteria the LLM scores (``llm`` is not false), in config order."""
    return [
        LLMCriterion(
            key=str(raw["key"]),
            title=str(raw.get("title") or raw["key"]),
            max=int(raw["max"]),
            requirements=str(raw.get("requirements") or "").strip(),
        )
        for raw in (grading_cfg or {}).get("teacher_criteria") or []
        if raw.get("llm") is not False
    ]


def subject_uses_llm(assignment_configs: Iterable[Mapping[str, Any] | None]) -> bool:
    return any(is_llm_graded(cfg) for cfg in assignment_configs)


def validate(code: str, a_cfg: Mapping[str, Any], read_text: Callable[[str], str | None]) -> None:
    """Raise ValueError unless the assignment's ``llm_grading`` block is usable."""
    block = a_cfg.get("llm_grading")
    where = f"assignment '{code}' llm_grading"
    if not isinstance(block, dict):
        raise ValueError(f"{where} must be a mapping")
    if not teacher_scores.is_scored_mode(a_cfg):
        raise ValueError(f"{where} requires review_mode '{teacher_scores.MODE}'")
    if not isinstance(block.get("enabled"), bool):
        raise ValueError(f"{where}.enabled must be true or false")
    if block.get("source") not in SOURCES:
        raise ValueError(f"{where}.source must be one of: {', '.join(sorted(SOURCES))}")

    task_file = block.get("task_file")
    if task_file:
        text = read_text(str(task_file))
        if text is None or not text.strip():
            raise ValueError(f"{where}.task_file '{task_file}' is missing or not readable text")
    elif not str(block.get("task") or "").strip():
        raise ValueError(f"{where} needs a non-empty 'task' or a 'task_file'")

    criteria = (a_cfg.get("grading") or {}).get("teacher_criteria") or []
    llm_ones = [c for c in criteria if c.get("llm") is not False]
    if not llm_ones:
        raise ValueError(f"{where} needs at least one criterion not marked llm: false")
    for c in llm_ones:
        if not str(c.get("requirements") or "").strip():
            raise ValueError(f"{where}: criterion '{c.get('key')}' needs non-empty 'requirements'")


def inline_task(a_cfg: dict[str, Any], read_text: Callable[[str], str | None]) -> None:
    """Replace ``task_file`` by its text in ``llm_grading.task`` (call after ``validate``)."""
    block = a_cfg.get("llm_grading") or {}
    task_file = block.get("task_file")
    if task_file:
        a_cfg["llm_grading"]["task"] = read_text(str(task_file))
