"""Provider-agnostic AI review client.

Exactly one provider is active at a time, selected by ``settings.ai_provider``.
Each provider takes a system prompt, a user prompt, and a JSON schema, and
returns the model's response parsed into a dict matching that schema. Callers
(``workers.tasks.review_tasks``) own prompt construction and result validation;
this module owns only the provider round-trip and JSON extraction.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from submissions_checker.core.config import Settings, get_settings
from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)


class AIProviderError(RuntimeError):
    """Raised when the active provider is misconfigured or returns unusable output."""


class AIProvider(Protocol):
    """Common surface for a single AI provider."""

    name: str
    model: str

    async def review(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Return the model's JSON response parsed against *schema*."""
        ...


def _parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response, tolerating ```json fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        # Strip a leading ```json / ``` fence and its trailing fence.
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text:
        raise AIProviderError("provider returned empty content")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIProviderError(f"provider returned non-JSON content: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("provider returned a non-object JSON value")
    return parsed


class OpenAIProvider:
    """Calls OpenAI's Chat Completions API in JSON mode."""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise AIProviderError("ai_provider=openai but OPENAI_API_KEY is not set")
        from openai import AsyncOpenAI

        self.model = settings.openai_model
        self._max_tokens = settings.ai_max_tokens
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=60.0,
        )

    async def review(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=self._max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return _parse_json(response.choices[0].message.content or "")


class AnthropicProvider:
    """Calls Anthropic's Messages API with a JSON-schema output constraint."""

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise AIProviderError("ai_provider=anthropic but ANTHROPIC_API_KEY is not set")
        from anthropic import AsyncAnthropic

        self.model = settings.anthropic_model
        self._max_tokens = settings.ai_max_tokens
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=60.0)

    async def review(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(
            (
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            ),
            "",
        )
        return _parse_json(text)


def get_ai_provider(settings: Settings | None = None) -> AIProvider:
    """Return the configured provider instance, or raise if it is unusable."""
    settings = settings or get_settings()
    if settings.ai_provider == "openai":
        return OpenAIProvider(settings)
    if settings.ai_provider == "anthropic":
        return AnthropicProvider(settings)
    # Settings validation already constrains ai_provider, but guard for safety.
    raise AIProviderError(f"unknown ai_provider: {settings.ai_provider!r}")
