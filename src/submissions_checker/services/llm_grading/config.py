"""``llm_grading`` assignment config block: helpers and validation.

Pure and DB-free so ``config_apply`` (validation, task inlining) and the grading runner
share one definition of "this assignment is graded by the LLM".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from submissions_checker.services import requirements, teacher_scores

SOURCES = frozenset({"google_classroom"})


@dataclass(frozen=True)
class LLMCriterion:
    key: str
    title: str
    max: int
    requirements: str


def is_llm_graded(assignment_config: Mapping[str, Any] | None) -> bool:
    cfg = assignment_config or {}
    if (cfg.get("llm_grading") or {}).get("enabled") is not True:
        return False
    try:
        return bool(requirements.from_config(cfg).llm_items())
    except ValueError:
        return False


def llm_criteria(assignment_config: Mapping[str, Any] | None) -> list[LLMCriterion]:
    """Score items the LLM grades (those carrying ``llm`` text), in config order."""
    if not assignment_config:
        return []
    return [
        LLMCriterion(key=i.key, title=i.title, max=int(i.points), requirements=i.llm or "")
        for i in requirements.from_config(assignment_config).llm_items()
    ]


def subject_uses_llm(assignment_configs: Iterable[Mapping[str, Any] | None]) -> bool:
    return any(is_llm_graded(cfg) for cfg in assignment_configs)


def validate(code: str, a_cfg: Mapping[str, Any], read_text: Callable[[str], str | None]) -> None:
    """Raise ValueError unless the assignment's ``llm_grading`` block is usable."""
    block = a_cfg.get("llm_grading")
    where = f"assignment '{code}' llm_grading"
    if not isinstance(block, dict):
        raise ValueError(f"{where} must be a mapping")
    legacy = "requirements" not in a_cfg
    if legacy and not teacher_scores.is_scored_mode(a_cfg):
        raise ValueError(f"{where} requires source 'classroom' (a requirements list)")
    if not legacy and requirements.parse(a_cfg).source != "classroom":
        raise ValueError(f"{where} requires source 'classroom'")
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

    if legacy:
        # An old criterion with blank text is dropped by the translation; name it here.
        for c in (a_cfg.get("grading") or {}).get("teacher_criteria") or []:
            if c.get("llm") is not False and not str(c.get("requirements") or "").strip():
                raise ValueError(
                    f"{where}: criterion '{c.get('key')}' needs non-empty 'requirements'"
                )
    if not requirements.from_config(a_cfg).llm_items():
        raise ValueError(f"{where} needs at least one score item with llm")


def inline_task(a_cfg: dict[str, Any], read_text: Callable[[str], str | None]) -> None:
    """Replace ``task_file`` by its text in ``llm_grading.task`` (call after ``validate``)."""
    block = a_cfg.get("llm_grading") or {}
    task_file = block.get("task_file")
    if task_file:
        a_cfg["llm_grading"]["task"] = read_text(str(task_file))
