"""Judge that delegates to the ``llm-judge`` sidecar (Claude Code CLI behind HTTP)."""

from __future__ import annotations

from typing import Any

import httpx

from submissions_checker.core.config import Settings
from submissions_checker.core.logging import get_logger
from submissions_checker.services.llm_grading.judge import (
    GradingRequest,
    GradingResult,
    JudgeError,
)
from submissions_checker.services.llm_grading.prompt import (
    SYSTEM_PROMPT,
    build_prompt,
    parse_result,
)

logger = get_logger(__name__)


class ClaudeCliJudge:
    name = "claude_cli"

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._url = settings.llm_judge_url.rstrip("/")
        self._token = settings.llm_judge_token
        self._model = settings.llm_judge_model
        self._http = http

    async def _post(self, req: GradingRequest, prompt: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        files = [("files", (f.name, f.content, f.mime)) for f in req.files]
        try:
            resp = await self._http.post(
                f"{self._url}/grade",
                data={"system": SYSTEM_PROMPT, "prompt": prompt, "model": self._model},
                files=files or None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise JudgeError(f"judge unreachable: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429:
            raise JudgeError("judge busy")
        if resp.status_code != 200:
            raise JudgeError(f"judge http {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise JudgeError("judge returned a non-JSON body") from exc
        if not isinstance(body, dict) or not isinstance(body.get("result"), str):
            raise JudgeError("judge body has no string 'result'")
        return body

    async def grade(self, req: GradingRequest) -> GradingResult:
        prompt = build_prompt(req)
        body = await self._post(req, prompt)
        try:
            return self._parse(body, req)
        except JudgeError as err:
            logger.warning("llm_grading_retry_invalid_answer", error=str(err))
            retry_prompt = (
                prompt
                + f"\n\nYour previous answer was invalid: {err}. "
                + "Reply with the JSON object only."
            )
            body = await self._post(req, retry_prompt)
            return self._parse(body, req)

    def _parse(self, body: dict[str, Any], req: GradingRequest) -> GradingResult:
        model = str(body.get("model") or self._model)
        result = parse_result(body["result"], req.criteria, self.name, model)
        usage = body.get("usage")
        if isinstance(usage, dict):
            result = GradingResult(
                result.criteria, result.comment, result.provider, result.model, usage
            )
        return result
