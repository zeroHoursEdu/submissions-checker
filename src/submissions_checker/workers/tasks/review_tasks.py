"""AI code review tasks using OpenAI."""

import asyncio
import json
import os
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from openai import AsyncOpenAI

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    Submission,
    SubmissionStatus,
)

logger = get_logger(__name__)


def extract_lab_id(submission: Submission) -> int:
    """Extract a numeric lab ID from the submission's head_ref branch name.

    Examples: 'lab_1' -> 1, 'lab-3-feature' -> 3. Falls back to 1 if no
    number is found.
    """
    match = re.search(r"\d+", getattr(submission, "head_ref", None) or "")
    if match:
        return int(match.group())
    logger.warning("extract_lab_id_fallback")
    return 1


async def collect_lab_data(path: str) -> tuple[str, str]:
    """Walk *path* in a thread and return (task_text, code_text).

    Reads README files as the task description and collects .py/.md/.txt
    source files as student code.
    """
    ignore_dirs = {".git", ".github"}
    allowed_extensions = {".py", ".md", ".txt"}

    if not Path(path).exists():
        return "Умова завдання не знайдена.", ""

    def _walk() -> tuple[str, str]:
        task_text = "Умова завдання не знайдена."
        code_text = ""
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, path)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if file.lower().startswith("readme"):
                        task_text = content
                    elif ext in allowed_extensions:
                        code_text += f"\n--- ФАЙЛ: {rel_path} ---\n{content}\n"
                except Exception:
                    continue
        return task_text, code_text

    return await asyncio.to_thread(_walk)


async def execute_review_task(db: AsyncSession, review_data: dict) -> None:  # type: ignore[type-arg]
    """Perform AI code review using OpenAI.

    All DB writes occur without an intermediate commit — the outbox processor
    commits the entire unit of work atomically after this function returns.
    After review, submission ends in REVIEWING status (teacher grades manually
    for PR-based submissions without a quiz template).
    """
    submission_id = review_data.get("submission_id")
    logger.info("execute_review_task_started", submission_id=submission_id)
    settings = get_settings()

    result = await db.execute(select(Submission).where(Submission.id == submission_id))
    submission = result.scalar_one()

    submission.status = SubmissionStatus.REVIEWING

    repo_path = submission.repository_path
    lab_id = extract_lab_id(submission)

    task_text, code_text = await collect_lab_data(repo_path or "")

    if not code_text:
        logger.warning("no_code_found", submission_id=submission_id)
        code_text = "# Код відсутній"

    theory = "Теорія відсутня."

    prompt = f"""
    Ти викладач Python. Перевір лабораторну роботу №{lab_id}.

    Ось затверджена теорія з лекції саме для цієї теми:
    {theory}

    Умова: {task_text}
    Код студента: {code_text}
    """

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=60.0)

    logger.info("calling_openai", submission_id=submission_id)
    response = await client.chat.completions.create(
        model=settings.openai_model,
        max_tokens=settings.ai_max_tokens,
        messages=[
            {
                "role": "system",
                "content": "Надай стислий огляд коду студента у форматі JSON: {\"review\": \"...\"}",
            },
            {"role": "user", "content": prompt},
        ],
    )

    ai_response_text = response.choices[0].message.content or ""
    ai_response_text = ai_response_text.strip()
    if ai_response_text.startswith("```"):
        ai_response_text = ai_response_text.split("```")[1]
        if ai_response_text.startswith("json"):
            ai_response_text = ai_response_text[4:]
        ai_response_text = ai_response_text.strip()

    if not ai_response_text:
        raise ValueError(
            f"OpenAI returned empty content (finish_reason={response.choices[0].finish_reason})"
        )

    parsed_review = json.loads(ai_response_text)
    submission.ai_review = parsed_review

    logger.info("execute_review_task_completed", submission_id=submission_id)


async def execute_ai_review_task(db: AsyncSession, payload: dict) -> None:  # type: ignore[type-arg]
    """AI review step in the new plugin-based check flow.

    Runs after tests pass. Transitions submission to AWAITING_TEACHER_REVIEW or COMPLETED
    depending on the next_step field in the payload.
    """
    submission_id = payload.get("submission_id")
    next_step = payload.get("next_step", "completed")
    logger.info("execute_ai_review_task_started", submission_id=submission_id)

    result = await db.execute(select(Submission).where(Submission.id == submission_id))
    submission = result.scalar_one()

    transition(submission, "start_ai_review")

    settings = get_settings()
    repo_path = submission.repository_path
    lab_id = extract_lab_id(submission)
    task_text, code_text = await collect_lab_data(repo_path or "")

    if not code_text:
        code_text = "# No code found"

    prompt = f"""
    You are a programming instructor. Review this student's code submission.

    Task: {task_text}
    Student code: {code_text}
    """

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=60.0)
    response = await client.chat.completions.create(
        model=settings.openai_model,
        max_tokens=settings.ai_max_tokens,
        messages=[
            {
                "role": "system",
                "content": 'Provide a concise code review in JSON format: {"review": "..."}',
            },
            {"role": "user", "content": prompt},
        ],
    )

    ai_response_text = (response.choices[0].message.content or "").strip()
    if ai_response_text.startswith("```"):
        ai_response_text = ai_response_text.split("```")[1]
        if ai_response_text.startswith("json"):
            ai_response_text = ai_response_text[4:]
        ai_response_text = ai_response_text.strip()

    if not ai_response_text:
        transition(submission, "ai_review_failed")
        raise ValueError(f"OpenAI returned empty content (finish_reason={response.choices[0].finish_reason})")

    parsed_review = json.loads(ai_response_text)
    submission.ai_review = parsed_review

    if next_step == "teacher":
        transition(submission, "ai_review_done_teacher")
    else:
        transition(submission, "ai_review_done_completed")

    logger.info("execute_ai_review_task_completed", submission_id=submission_id)
