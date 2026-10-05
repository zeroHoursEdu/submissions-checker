import json

import httpx
import pytest

from submissions_checker.core.config import Settings
from submissions_checker.services.llm_grading.claude_cli import ClaudeCliJudge
from submissions_checker.services.llm_grading.judge import (
    GradingRequest,
    JudgeCriterion,
    JudgeError,
    WorkFile,
    get_judge,
)

CRIT = [JudgeCriterion("code", "Код", 3, "Сервер працює")]
REQ = GradingRequest(
    task="T", instructions="", criteria=CRIT, files=[WorkFile("a.txt", b"hello", "text/plain")]
)
GOOD = json.dumps(
    {"criteria": {"code": {"points": 2, "justification": "j", "evidence": "e"}}, "comment": "c"}
)


def _judge(handler) -> ClaudeCliJudge:
    settings = Settings(
        llm_judge_url="http://judge:8090/", llm_judge_token="tok", llm_judge_model="opus"
    )
    return ClaudeCliJudge(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ok(result: str = GOOD):
    return httpx.Response(200, json={"result": result, "model": "claude-x", "usage": {"in": 1}})


async def test_sends_bearer_multipart_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return _ok()

    res = await _judge(handler).grade(REQ)
    assert seen["url"] == "http://judge:8090/grade"
    assert seen["auth"] == "Bearer tok"
    for part in (
        b'name="system"',
        b'name="prompt"',
        b'name="model"',
        b"opus",
        b'filename="a.txt"',
        b"hello",
    ):
        assert part in seen["body"]
    assert res.criteria["code"].points == 2
    assert (res.provider, res.model, res.usage) == ("claude_cli", "claude-x", {"in": 1})


async def test_retries_once_on_invalid_answer():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return _ok("not json") if len(bodies) == 1 else _ok()

    res = await _judge(handler).grade(REQ)
    assert len(bodies) == 2
    assert b"Your previous answer was invalid" in bodies[1]
    assert b"Your previous answer was invalid" not in bodies[0]
    assert res.criteria["code"].points == 2


async def test_raises_after_two_invalid():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _ok("still not json")

    with pytest.raises(JudgeError):
        await _judge(handler).grade(REQ)
    assert len(calls) == 2


async def test_429_is_busy_without_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="busy")

    with pytest.raises(JudgeError, match="judge busy"):
        await _judge(handler).grade(REQ)
    assert len(calls) == 1


async def test_non_200_includes_status_and_text():
    with pytest.raises(JudgeError, match="judge http 500: boom"):
        await _judge(lambda r: httpx.Response(500, text="boom")).grade(REQ)


async def test_transport_error_becomes_judge_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(JudgeError, match="unreachable"):
        await _judge(handler).grade(REQ)


def test_get_judge_returns_claude_cli():
    assert get_judge(Settings()).name == "claude_cli"
