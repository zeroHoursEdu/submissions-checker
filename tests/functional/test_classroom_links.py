"""Teacher resolution of Classroom roster entries: link, ignore, confirm, confirm-all."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.core.config import Settings, get_settings
from submissions_checker.db.models import Subject, SubjectsAssignment
from submissions_checker.db.models.audit_log import AuditLog
from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import (
    ClassroomLinkMethod,
    LLMGradingStatus,
    UserRole,
)
from submissions_checker.db.models.subject import SubjectsStudents
from submissions_checker.main import app
from submissions_checker.services.google.links import link_state_for_students

pytestmark = pytest.mark.asyncio

LLM_CFG = {"review_mode": "quiz_and_teacher_scores", "llm_grading": {"enabled": True}}


@pytest.fixture(autouse=True)
def _keep_loaded(db):
    # Tests read ids off objects after several commits; avoid lazy refresh outside greenlets.
    db.sync_session.expire_on_commit = False


@pytest.fixture(autouse=True)
def google_env():
    app.dependency_overrides[get_settings] = lambda: Settings(
        secret_key="test-secret-key-minimum-32-chars-long",
        google_client_id="cid",
        google_client_secret="csecret",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    yield
    app.dependency_overrides.pop(get_settings, None)


async def _subject(db, teacher):
    subject = Subject(name="S", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    asg = SubjectsAssignment(subject_id=subject.id, title="Lab", code="lab", config=LLM_CFG)
    db.add(asg)
    await db.commit()
    return subject, asg


async def _enrol(db, subject, student):
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    await db.commit()


async def _link(db, subject, uid, *, method, student=None, score=None, candidates=None, **kw):
    link = ClassroomStudentLink(
        subject_id=subject.id,
        classroom_user_id=uid,
        classroom_email=f"{uid}@x.ua",
        classroom_name=f"ІП-43 Name {uid}",
        student_id=student.id if student else None,
        method=method.value,
        score=score,
        candidates=candidates,
        confirmed=kw.get("confirmed", False),
    )
    db.add(link)
    await db.commit()
    return link


async def _waiting_grading(db, asg, link):
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=link.id,
        classroom_submission_id=f"s-{link.id}",
        state="TURNED_IN",
        content_hash="h" * 64,
        manifest=[],
        seen_at=datetime.now(UTC),
    )
    db.add(work)
    await db.commit()
    grading = LLMGrading(classroom_work_id=work.id, status=LLMGradingStatus.WAITING_LINK.value)
    db.add(grading)
    await db.commit()
    return grading


def _url(subject, link=None, suffix=""):
    base = f"/teacher/subjects/{subject.id}/classroom/links"
    return f"{base}/{link.id}" if link else f"{base}/{suffix}"


async def _refresh(db, obj):
    await db.refresh(obj)
    return obj


async def test_unmatched_panel_lists_none_links_with_suggestions(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, _ = await _subject(db, teacher)
    st = await make_student(full_name="Комін Тарас")
    await _enrol(db, subject, st)
    await _link(
        db,
        subject,
        "u1",
        method=ClassroomLinkMethod.NONE,
        candidates=[{"student_id": st.id, "full_name": "Комін Тарас", "score": 0.7}],
    )
    await _link(db, subject, "u2", method=ClassroomLinkMethod.EMAIL, student=st, confirmed=True)
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert resp.status_code == 200
    assert "classroom-unmatched" in resp.text
    assert "ІП-43 Name u1" in resp.text
    assert "u1@x.ua" in resp.text
    assert "ІП-43 Name u2" not in resp.text
    assert f'name="student_id" value="{st.id}"' in resp.text
    assert "Комін Тарас (70%)" in resp.text


async def test_manual_link_releases_waiting_gradings(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg = await _subject(db, teacher)
    st = await make_student()
    await _enrol(db, subject, st)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    grading = await _waiting_grading(db, asg, link)
    r = await teacher_client.post(
        _url(subject, link), data={"action": "link", "student_id": str(st.id)}
    )
    assert r.status_code == 303
    link = await _refresh(db, link)
    assert (link.method, link.confirmed, link.student_id) == ("MANUAL", True, st.id)
    assert (await _refresh(db, grading)).status == LLMGradingStatus.PENDING.value


async def test_link_rejects_unenrolled_student(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, _ = await _subject(db, teacher)
    st = await make_student()
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    r = await teacher_client.post(
        _url(subject, link), data={"action": "link", "student_id": str(st.id)}
    )
    assert r.status_code == 422
    assert (await _refresh(db, link)).method == "NONE"


async def test_link_rejects_student_already_linked(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, _ = await _subject(db, teacher)
    st = await make_student()
    await _enrol(db, subject, st)
    await _link(db, subject, "u1", method=ClassroomLinkMethod.EMAIL, student=st, confirmed=True)
    other = await _link(db, subject, "u2", method=ClassroomLinkMethod.NONE)
    r = await teacher_client.post(
        _url(subject, other), data={"action": "link", "student_id": str(st.id)}
    )
    assert r.status_code == 409
    assert (await _refresh(db, other)).method == "NONE"


async def test_ignore_hides_from_panel(teacher_client: AsyncClient, db, teacher, make_student):
    subject, asg = await _subject(db, teacher)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    grading = await _waiting_grading(db, asg, link)
    r = await teacher_client.post(_url(subject, link), data={"action": "ignore"})
    assert r.status_code == 303
    link = await _refresh(db, link)
    assert (link.method, link.confirmed, link.student_id) == ("IGNORED", True, None)
    assert (await _refresh(db, grading)).status == LLMGradingStatus.WAITING_LINK.value
    resp = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "ІП-43 Name u1" not in resp.text


async def test_confirm_name_link(teacher_client: AsyncClient, db, teacher, make_student):
    subject, _ = await _subject(db, teacher)
    st = await make_student()
    await _enrol(db, subject, st)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NAME, student=st, score=0.9)
    r = await teacher_client.post(_url(subject, link), data={"action": "confirm"})
    assert r.status_code == 303
    link = await _refresh(db, link)
    assert (link.method, link.confirmed, link.student_id) == ("MANUAL", True, st.id)


async def test_confirm_rejected_for_non_name_link(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, _ = await _subject(db, teacher)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    r = await teacher_client.post(_url(subject, link), data={"action": "confirm"})
    assert r.status_code == 303
    assert "classroom_error=not_name_match" in r.headers["location"]
    assert (await _refresh(db, link)).method == "NONE"


async def test_confirm_all_only_above_threshold(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, _ = await _subject(db, teacher)
    s1, s2 = await make_student(), await make_student()
    for s in (s1, s2):
        await _enrol(db, subject, s)
    hi = await _link(db, subject, "u1", method=ClassroomLinkMethod.NAME, student=s1, score=0.97)
    lo = await _link(db, subject, "u2", method=ClassroomLinkMethod.NAME, student=s2, score=0.88)
    page = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "links/confirm-all" in page.text
    r = await teacher_client.post(_url(subject, suffix="confirm-all"))
    assert r.status_code == 303
    assert (await _refresh(db, hi)).method == "MANUAL"
    assert (await _refresh(db, hi)).confirmed is True
    assert (await _refresh(db, lo)).method == "NAME"
    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "classroom_link_confirm_all"))
    ).scalar_one()
    assert log.detail["count"] == 1


async def test_link_actions_write_audit(teacher_client: AsyncClient, db, teacher, make_student):
    subject, _ = await _subject(db, teacher)
    st = await make_student()
    await _enrol(db, subject, st)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    await teacher_client.post(
        _url(subject, link), data={"action": "link", "student_id": str(st.id)}
    )
    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "classroom_link_link"))
    ).scalar_one()
    assert log.target_type == "classroom_student_link"
    assert log.target_id == link.id
    assert log.detail["student_id"] == st.id


async def test_other_teacher_forbidden(teacher_client: AsyncClient, db, make_user):
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    subject, _ = await _subject(db, owner)
    link = await _link(db, subject, "u1", method=ClassroomLinkMethod.NONE)
    r = await teacher_client.post(_url(subject, link), data={"action": "ignore"})
    assert r.status_code in (403, 404)
    r = await teacher_client.post(_url(subject, suffix="confirm-all"))
    assert r.status_code in (403, 404)
    assert (await _refresh(db, link)).method == "NONE"


async def test_link_state_prefers_confirmed_then_newest(db, teacher, make_student):
    subject, _ = await _subject(db, teacher)
    st = await make_student()
    await _link(db, subject, "u1", method=ClassroomLinkMethod.NAME, student=st, score=0.9)
    confirmed = await _link(
        db, subject, "u2", method=ClassroomLinkMethod.MANUAL, student=st, confirmed=True
    )
    await _link(db, subject, "u3", method=ClassroomLinkMethod.NAME, student=st, score=0.9)
    state = await link_state_for_students(db, subject.id)
    assert state[st.id].id == confirmed.id
