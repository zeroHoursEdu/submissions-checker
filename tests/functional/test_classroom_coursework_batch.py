"""One form links every LLM-graded assignment to its Classroom coursework."""

from __future__ import annotations

import html
import re

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes.teacher_classroom import classroom_http
from submissions_checker.core.config import get_settings
from submissions_checker.db.models import Subject, SubjectsAssignment
from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.enums import UserRole
from submissions_checker.main import app
from tests.functional.test_classroom_connect import LLM_CFG, _connect, _settings

pytestmark = pytest.mark.asyncio

COURSEWORK = [
    {"id": "w2", "title": "2. Графічний інтерфейс"},
    {"id": "w1", "title": "1. Системна інформація"},
    {"id": "wx", "title": "Оголошення"},
]


def _google(request: httpx.Request) -> httpx.Response:
    if str(request.url).endswith("/token"):
        return httpx.Response(200, json={"access_token": "at"})
    if "/courseWork" in str(request.url):
        return httpx.Response(200, json={"courseWork": COURSEWORK})
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def google_env():
    app.dependency_overrides[get_settings] = lambda: _settings()

    async def _http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_google)) as c:
            yield c

    app.dependency_overrides[classroom_http] = _http
    yield
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(classroom_http, None)


@pytest.fixture(autouse=True)
def _keep_loaded(db):
    db.sync_session.expire_on_commit = False


async def _world(db, teacher):
    subject = Subject(name="S", owner_id=teacher.id, classroom_course_id="c1")
    db.add(subject)
    await db.commit()
    lab1 = SubjectsAssignment(
        subject_id=subject.id, title="ЛР №1 — Системна інформація", code="lab1", config=LLM_CFG
    )
    lab2 = SubjectsAssignment(
        subject_id=subject.id, title="ЛР №2 — GDI", code="lab2", config=LLM_CFG
    )
    quiz = SubjectsAssignment(subject_id=subject.id, title="Тест 1", code="quiz", config={})
    db.add_all([lab1, lab2, quiz])
    await db.commit()
    await _connect(db, teacher)
    return subject, lab1, lab2, quiz


def _url(subject) -> str:
    return f"/teacher/subjects/{subject.id}/classroom/coursework/batch"


def _select(page: str, asg) -> str:
    m = re.search(rf'<select name="coursework_{asg.id}".*?</select>', page, re.S)
    assert m, f"no select for assignment {asg.id}"
    return m.group(0)


async def test_card_prefills_guesses_in_one_form(teacher_client: AsyncClient, db, teacher):
    subject, lab1, lab2, quiz = await _world(db, teacher)
    page = (await teacher_client.get(f"/teacher/subjects/{subject.id}")).text

    assert page.count(f'action="{_url(subject)}"') == 1
    assert re.search(r'<option value="w1"[^>]*selected', _select(page, lab1))
    assert re.search(r'<option value="w2"[^>]*selected', _select(page, lab2))
    assert "запропоновано" in html.unescape(page)
    assert f'name="coursework_{quiz.id}"' not in page


async def test_existing_link_wins_over_a_guess(teacher_client: AsyncClient, db, teacher):
    subject, lab1, _, _ = await _world(db, teacher)
    lab1.classroom_coursework_id = "wx"
    lab1.classroom_coursework_title = "Оголошення"
    await db.commit()
    page = (await teacher_client.get(f"/teacher/subjects/{subject.id}")).text
    assert re.search(r'<option value="wx"[^>]*selected', _select(page, lab1))


async def test_batch_saves_every_row(teacher_client: AsyncClient, db, teacher):
    subject, lab1, lab2, _ = await _world(db, teacher)
    r = await teacher_client.post(
        _url(subject), data={f"coursework_{lab1.id}": "w1", f"coursework_{lab2.id}": "w2"}
    )
    assert r.status_code == 303
    assert "classroom=coursework_linked" in r.headers["location"]
    await db.refresh(lab1)
    await db.refresh(lab2)
    assert (lab1.classroom_coursework_id, lab1.classroom_coursework_title) == (
        "w1",
        "1. Системна інформація",
    )
    assert lab2.classroom_coursework_id == "w2"
    logs = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "classroom_coursework_linked")))
        .scalars()
        .all()
    )
    assert sorted(log.target_id for log in logs) == sorted([lab1.id, lab2.id])


async def test_batch_empty_value_unlinks(teacher_client: AsyncClient, db, teacher):
    subject, lab1, _, _ = await _world(db, teacher)
    lab1.classroom_coursework_id = "w1"
    await db.commit()
    await teacher_client.post(_url(subject), data={f"coursework_{lab1.id}": ""})
    await db.refresh(lab1)
    assert lab1.classroom_coursework_id is None


async def test_batch_with_a_duplicate_saves_nothing(teacher_client: AsyncClient, db, teacher):
    subject, lab1, lab2, _ = await _world(db, teacher)
    r = await teacher_client.post(
        _url(subject), data={f"coursework_{lab1.id}": "w1", f"coursework_{lab2.id}": "w1"}
    )
    assert r.headers["location"].endswith("classroom_error=coursework_taken")
    await db.refresh(lab1)
    assert lab1.classroom_coursework_id is None


async def test_batch_duplicate_with_an_untouched_row(teacher_client: AsyncClient, db, teacher):
    subject, lab1, lab2, _ = await _world(db, teacher)
    lab1.classroom_coursework_id = "w1"
    await db.commit()
    r = await teacher_client.post(_url(subject), data={f"coursework_{lab2.id}": "w1"})
    assert r.headers["location"].endswith("classroom_error=coursework_taken")


async def test_batch_rejects_unknown_coursework(teacher_client: AsyncClient, db, teacher):
    subject, lab1, lab2, _ = await _world(db, teacher)
    r = await teacher_client.post(
        _url(subject), data={f"coursework_{lab1.id}": "w1", f"coursework_{lab2.id}": "nope"}
    )
    assert r.status_code == 422
    await db.refresh(lab1)
    assert lab1.classroom_coursework_id is None


async def test_batch_ignores_non_llm_assignments(teacher_client: AsyncClient, db, teacher):
    subject, _, _, quiz = await _world(db, teacher)
    await teacher_client.post(_url(subject), data={f"coursework_{quiz.id}": "w1"})
    await db.refresh(quiz)
    assert quiz.classroom_coursework_id is None


async def test_other_teacher_cannot_batch(teacher_client: AsyncClient, db, make_user):
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    subject, lab1, _, _ = await _world(db, owner)
    r = await teacher_client.post(_url(subject), data={f"coursework_{lab1.id}": "w1"})
    assert r.status_code in (403, 404)
