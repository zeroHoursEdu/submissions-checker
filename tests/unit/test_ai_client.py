"""Contract tests for the AI layer (AIClient + CodeReviewer).

Both are documented skeletons that raise NotImplementedError. No OpenAI SDK call
is ever made (the skeleton never builds a client), so there is nothing to patch
at the network boundary yet — these tests pin the current contract and assert the
client wires settings in on construction so the real impl has the config it needs.
"""

from __future__ import annotations

import pytest

from submissions_checker.services.ai.client import AIClient
from submissions_checker.services.ai.code_reviewer import CodeReviewer
from submissions_checker.core.config import get_settings


def test_ai_client_loads_settings_on_init() -> None:
    client = AIClient()
    # Settings is the source of model / token / temperature config the real
    # impl will pass to the OpenAI SDK.
    assert client.settings is get_settings()
    assert hasattr(client.settings, "openai_model")
    assert hasattr(client.settings, "ai_max_tokens")
    assert hasattr(client.settings, "ai_temperature")


async def test_review_code_not_implemented() -> None:
    client = AIClient()
    with pytest.raises(NotImplementedError):
        await client.review_code("print('hi')", context="lab1")


async def test_review_code_accepts_empty_context() -> None:
    client = AIClient()
    with pytest.raises(NotImplementedError):
        await client.review_code("x = 1")


async def test_analyze_test_results_not_implemented() -> None:
    client = AIClient()
    with pytest.raises(NotImplementedError):
        await client.analyze_test_results("3 passed, 1 failed")


async def test_code_reviewer_review_submission_not_implemented() -> None:
    reviewer = CodeReviewer()
    with pytest.raises(NotImplementedError):
        await reviewer.review_submission(submission_id=42)
