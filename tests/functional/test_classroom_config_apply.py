"""apply-config accepts the llm_grading block: task_file inlining, validation, re-apply."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from tests.functional.test_apply_config import _make_zip, _post, _redirect_query, _scored_config

pytestmark = pytest.mark.asyncio

TASK_TEXT = "Зробіть лабораторну роботу №1"


def _llm_config(requirements: str = "Є висновки") -> dict[str, Any]:
    cfg = _scored_config(16)
    lab = cfg["assignments"]["lab1"]
    lab["grading"]["teacher_criteria"][0]["requirements"] = requirements
    lab["grading"]["teacher_criteria"][1]["llm"] = False
    lab["llm_grading"] = {
        "enabled": True,
        "source": "google_classroom",
        "task_file": "tasks/l1.md",
    }
    return cfg


def _files(text: str = TASK_TEXT) -> dict[str, bytes]:
    return {"tasks/l1.md": text.encode()}


async def test_apply_inlines_task_file(teacher_client: AsyncClient, db: AsyncSession) -> None:
    resp = await _post(teacher_client, _make_zip(_llm_config(), _files()))
    assert _redirect_query(resp)["apply_result"] == ["created"]
    asg = (await db.execute(select(SubjectsAssignment))).scalar_one()
    assert asg.config["llm_grading"]["task"] == TASK_TEXT


async def test_apply_rejects_llm_on_tests_only(
    teacher_client: AsyncClient, db: AsyncSession
) -> None:
    cfg = _llm_config()
    cfg["assignments"]["lab1"]["review_mode"] = "tests_only"
    resp = await _post(teacher_client, _make_zip(cfg, _files()))
    assert "quiz_and_teacher_scores" in urllib.parse.unquote(resp.headers["location"])
    assert (await db.execute(select(func.count()).select_from(Subject))).scalar_one() == 0


async def test_reapply_keeps_coursework_link(teacher_client: AsyncClient, db: AsyncSession) -> None:
    await _post(teacher_client, _make_zip(_llm_config(), _files()))
    asg = (await db.execute(select(SubjectsAssignment))).scalar_one()
    asg.classroom_coursework_id = "w1"
    asg.classroom_coursework_title = "ЛР1"
    await db.commit()

    resp = await _post(teacher_client, _make_zip(_llm_config("Інші вимоги"), _files("Нове")))
    assert _redirect_query(resp)["apply_result"] == ["updated"]
    await db.refresh(asg)
    assert asg.classroom_coursework_id == "w1"
    assert asg.config["llm_grading"]["task"] == "Нове"
