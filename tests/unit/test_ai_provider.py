"""Unit tests for the AI provider factory (``services.ai.provider``).

The factory selects exactly one provider from settings and fails clearly when
the active provider's API key is unset. Provider ``__init__`` checks the key
*before* importing the SDK, so the missing-key paths need no SDK installed.
"""

from __future__ import annotations

import pytest

from submissions_checker.core.config import Settings
from submissions_checker.services.ai.provider import (
    AIProviderError,
    AnthropicProvider,
    OpenAIProvider,
    get_ai_provider,
)

DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
STRONG_KEY = "f3a1c9e7b5d2486017aa44bc99ee1122aabbccddeeff00112233445566778899"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"secret_key": STRONG_KEY, "database_url": DB_URL}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_openai_selected_when_key_present() -> None:
    provider = get_ai_provider(_settings(ai_provider="openai", openai_api_key="sk-test"))
    assert isinstance(provider, OpenAIProvider)
    assert provider.name == "openai"


def test_openai_missing_key_raises() -> None:
    with pytest.raises(AIProviderError, match="OPENAI_API_KEY"):
        get_ai_provider(_settings(ai_provider="openai", openai_api_key=None))


def test_anthropic_missing_key_raises() -> None:
    with pytest.raises(AIProviderError, match="ANTHROPIC_API_KEY"):
        get_ai_provider(_settings(ai_provider="anthropic", anthropic_api_key=None))


def test_anthropic_selected_when_key_present() -> None:
    pytest.importorskip("anthropic")
    provider = get_ai_provider(
        _settings(ai_provider="anthropic", anthropic_api_key="sk-ant-test")
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"
