"""LLM judge interface: request/response dataclasses, the protocol and the factory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from submissions_checker.core.config import Settings


class JudgeError(RuntimeError):
    """Raised when the judge is unreachable, busy, or returns an unusable answer."""


@dataclass(frozen=True)
class JudgeCriterion:
    key: str
    title: str
    max: int
    requirements: str


@dataclass(frozen=True)
class WorkFile:
    name: str
    content: bytes
    mime: str


@dataclass(frozen=True)
class GradingRequest:
    task: str
    instructions: str
    criteria: list[JudgeCriterion]
    files: list[WorkFile]


@dataclass(frozen=True)
class CriterionVerdict:
    key: str
    points: int
    justification: str
    evidence: str


@dataclass(frozen=True)
class GradingResult:
    criteria: dict[str, CriterionVerdict]
    comment: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


class LLMJudge(Protocol):
    name: str

    async def grade(self, req: GradingRequest) -> GradingResult: ...


def get_judge(settings: Settings) -> LLMJudge:
    """Return the judge selected by ``settings.llm_judge_provider``."""
    if settings.llm_judge_provider == "claude_cli":
        import httpx

        from submissions_checker.services.llm_grading.claude_cli import ClaudeCliJudge

        return ClaudeCliJudge(settings, httpx.AsyncClient(timeout=settings.llm_judge_timeout))
    raise JudgeError(f"unknown llm_judge_provider: {settings.llm_judge_provider}")
