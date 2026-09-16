"""Read an AI review verdict the way the UI and the review task need it.

The verdict dict is produced by workers.tasks.review_tasks and stored on
``Submission.ai_review``. Whether it counts as *flagged* depends on per-assignment
thresholds in the ``ai_review`` config block, so both the worker (routing to teacher
review) and the teacher pages (badges) go through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class VerdictSummary:
    cheating: bool
    ai_generated: bool
    cheating_confidence: float
    ai_generated_confidence: float
    cheating_reason: str
    ai_generated_reason: str
    code_mark: int | None
    comment: str
    provider: str | None
    model: str | None

    @property
    def flagged(self) -> bool:
        return self.cheating or self.ai_generated


def _hit(block: Any, flag_key: str, threshold: float) -> tuple[bool, float, str]:
    if not isinstance(block, dict):
        return False, 0.0, ""
    try:
        confidence = float(block.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    hit = bool(block.get(flag_key)) and confidence >= threshold
    return hit, confidence, str(block.get("reason") or "")


def summarize(
    verdict: dict[str, Any] | None, ai_review_cfg: dict[str, Any] | None
) -> VerdictSummary | None:
    if not verdict:
        return None
    cfg = ai_review_cfg or {}
    cheat_thr = float(cfg.get("cheating_threshold", DEFAULT_THRESHOLD))
    aigen_thr = float(cfg.get("ai_generated_threshold", DEFAULT_THRESHOLD))
    cheating, cheat_conf, cheat_reason = _hit(verdict.get("cheating"), "is_cheating", cheat_thr)
    aigen, aigen_conf, aigen_reason = _hit(
        verdict.get("ai_generated"), "is_ai_generated", aigen_thr
    )
    raw_mark = verdict.get("code_mark")
    code_mark = int(raw_mark) if isinstance(raw_mark, int | float) else None
    return VerdictSummary(
        cheating=cheating,
        ai_generated=aigen,
        cheating_confidence=cheat_conf,
        ai_generated_confidence=aigen_conf,
        cheating_reason=cheat_reason,
        ai_generated_reason=aigen_reason,
        code_mark=code_mark,
        comment=str(verdict.get("comment") or ""),
        provider=verdict.get("provider"),
        model=verdict.get("model"),
    )


def is_flagged(verdict: dict[str, Any], ai_review_cfg: dict[str, Any]) -> bool:
    """True if cheating or AI-generated confidence meets the configured threshold."""
    summary = summarize(verdict, ai_review_cfg)
    return bool(summary and summary.flagged)
