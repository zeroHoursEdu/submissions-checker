"""Assignment board for LLM-graded assignments: draft pre-fill, badges, details, retry,
file downloads and the approval gate in teacher_save_scores."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.routes import teacher_classroom
from submissions_checker.db.models import (
    AuditLog,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
)
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
from submissions_checker.services import squads
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio

GRADING: dict[str, Any] = {
    "quiz_points": 8,
    "teacher_criteria": [
        {"key": "report", "title": "Звіт", "max": 5, "requirements": "r"},
        {"key": "star", "title": "Зірочка", "max": 3, "optional": True, "llm": False},
    ],
}
LLM_CFG: dict[str, Any] = {
    "review_mode": "quiz_and_teacher_scores",
    "grading": GRADING,
    "llm_grading": {"enabled": True, "source": "google_classroom", "task": "t"},
}
PLAIN_CFG: dict[str, Any] = {"review_mode": "quiz_and_teacher_scores", "grading": GRADING}

DRAFT_V1 = {
    "criteria": {
        "report": {"points": 4, "justification": "Звіт майже повний", "evidence": "розділ 2"}
    },
    "comment": "Загалом добре",
    "usage": {},
}


@pytest.fixture(autouse=True)
def _keep_loaded(db):
    db.sync_session.expire_on_commit = False


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _world(db, teacher, make_student, *, config=LLM_CFG, squad_max_size=None):
    subject = Subject(name="S", owner_id=teacher.id, squad_max_size=squad_max_size)
    db.add(subject)
    await db.commit()
    asg = SubjectsAssignment(
        subject_id=subject.id, title="Lab", code="lab", min_grade=0, max_grade=16, config=config
    )
    db.add(asg)
    await db.commit()
    student = await make_student(full_name="Комін Тарас")
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    await db.commit()
    return subject, asg, student


async def _link(db, subject, student, *, method=ClassroomLinkMethod.EMAIL, confirmed=True):
    link = ClassroomStudentLink(
        subject_id=subject.id,
        classroom_user_id=f"u{student.id}",
        classroom_email=f"u{student.id}@x.ua",
        classroom_name=f"ІП-43 Komin Taras {student.id}",
        student_id=student.id,
        method=method.value,
        confirmed=confirmed,
    )
    db.add(link)
    await db.commit()
    return link


async def _work(
    db,
    asg,
    link,
    *,
    status=LLMGradingStatus.DONE,
    draft=DRAFT_V1,
    seen_at=None,
    manifest=None,
    content_hash="a",
    **grading_kw,
):
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=link.id,
        classroom_submission_id=f"s-{link.id}",
        state="TURNED_IN",
        late=False,
        content_hash=content_hash * 64,
        manifest=manifest
        if manifest is not None
        else [
            {
                "drive_id": "d1",
                "name": "звіт.pdf",
                "mime": "application/pdf",
                "size": 3,
                "modified": "",
                "sha256": "x",
                "storage_key": "k/report.pdf",
                "skipped": None,
            }
        ],
        seen_at=seen_at or datetime.now(UTC),
    )
    db.add(work)
    await db.commit()
    grading = LLMGrading(
        classroom_work_id=work.id,
        status=status.value,
        draft=draft if status == LLMGradingStatus.DONE else None,
        **grading_kw,
    )
    db.add(grading)
    await db.commit()
    return work, grading


def _board(subject, asg) -> str:
    return f"/teacher/subjects/{subject.id}/assignments/{asg.id}"


def _input(html: str, student_id: int, key: str) -> str:
    m = re.search(rf'<input form="scores-{student_id}"[^>]*name="score_{key}"[^>]*>', html)
    assert m, f"no input for {key}"
    return m.group(0)


# ── Board rendering ──────────────────────────────────────────────────────────


async def test_board_prefills_draft_when_no_teacher_scores(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    await _work(db, asg, await _link(db, subject, st))

    page = await teacher_client.get(_board(subject, asg))

    assert page.status_code == 200
    inp = _input(page.text, st.id, "report")
    assert 'value="4"' in inp
    assert "AI-чернетка" in inp
    # The non-LLM criterion has no draft value.
    assert 'value=""' in _input(page.text, st.id, "star")
    # Details: justification, evidence, comment, a link to the file.
    assert "Звіт майже повний" in page.text
    assert "розділ 2" in page.text
    assert "Загалом добре" in page.text
    assert "звіт.pdf" in page.text
    assert re.search(rf"/teacher/subjects/{subject.id}/classroom/works/\d+/files/0", page.text)


async def test_board_keeps_teacher_scores_over_draft(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    db.add(
        StudentAssignment(
            student_id=st.id, subjects_assignment_id=asg.id, teacher_scores={"report": 2}
        )
    )
    await db.commit()
    await _work(db, asg, await _link(db, subject, st))

    page = await teacher_client.get(_board(subject, asg))

    inp = _input(page.text, st.id, "report")
    assert 'value="2"' in inp
    assert "AI-чернетка" not in inp


async def test_board_shows_pending_and_failed_badges(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    other = await make_student(full_name="Білик Олена")
    db.add(SubjectsStudents(subject_id=subject.id, student_id=other.id))
    await db.commit()
    await _work(db, asg, await _link(db, subject, st), status=LLMGradingStatus.PENDING)
    _, failed = await _work(
        db,
        asg,
        await _link(db, subject, other),
        status=LLMGradingStatus.FAILED,
        content_hash="b",
        error="judge exploded",
        attempts=3,
    )

    page = await teacher_client.get(_board(subject, asg))

    assert "очікує оцінювання" in page.text
    assert "помилка оцінювання" in page.text
    assert (
        f'formaction="/teacher/subjects/{subject.id}/classroom/gradings/{failed.id}/retry"'
        in page.text
    )


async def test_board_unchanged_for_non_llm_scored_assignment(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student, config=PLAIN_CFG)
    # Even a stray link + work must not surface on a non-LLM board.
    await _work(
        db, asg, await _link(db, subject, st, method=ClassroomLinkMethod.NAME, confirmed=False)
    )

    page = await teacher_client.get(_board(subject, asg))

    assert page.status_code == 200
    assert 'value=""' in _input(page.text, st.id, "report")
    for marker in ("AI-чернетка", "data-llm-details", "очікує оцінювання", "збіг за іменем"):
        assert marker not in page.text


async def test_squad_member_sees_mate_draft(teacher_client: AsyncClient, db, teacher, make_student):
    subject, asg, st = await _world(db, teacher, make_student, squad_max_size=2)
    mate = await make_student(full_name="Білик Олена")
    db.add(SubjectsStudents(subject_id=subject.id, student_id=mate.id))
    await db.commit()
    await squads.teacher_assign(db, subject.id, teacher.id, [st.id, mate.id])
    await db.commit()
    await _work(db, asg, await _link(db, subject, st))

    page = await teacher_client.get(_board(subject, asg))

    for sid in (st.id, mate.id):
        inp = _input(page.text, sid, "report")
        assert 'value="4"' in inp and "AI-чернетка" in inp


async def test_name_match_badge_offers_confirm(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    link = await _link(db, subject, st, method=ClassroomLinkMethod.NAME, confirmed=False)
    await _work(db, asg, link)

    page = await teacher_client.get(_board(subject, asg))

    assert "збіг за іменем" in page.text
    assert f'formaction="/teacher/subjects/{subject.id}/classroom/links/{link.id}"' in page.text


# ── Retry ────────────────────────────────────────────────────────────────────


async def test_retry_resets_failed(teacher_client: AsyncClient, db, teacher, make_student):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(
        db,
        asg,
        await _link(db, subject, st),
        status=LLMGradingStatus.FAILED,
        attempts=3,
        error="boom",
    )

    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/classroom/gradings/{grading.id}/retry",
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == _board(subject, asg)
    await db.refresh(grading)
    assert grading.status == LLMGradingStatus.PENDING.value
    assert grading.attempts == 0
    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "llm_grading_retry"))
    ).scalar_one()
    assert log.target_id == grading.id


async def test_retry_rejects_foreign_subject(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(
        db, asg, await _link(db, subject, st), status=LLMGradingStatus.FAILED, attempts=3
    )
    other_subject = Subject(name="Other", owner_id=teacher.id)
    db.add(other_subject)
    await db.commit()

    r = await teacher_client.post(
        f"/teacher/subjects/{other_subject.id}/classroom/gradings/{grading.id}/retry",
        follow_redirects=False,
    )

    assert r.status_code == 404
    await db.refresh(grading)
    assert grading.status == LLMGradingStatus.FAILED.value


# ── Save gate + approval ─────────────────────────────────────────────────────


def _scores_url(subject, asg) -> str:
    return f"/teacher/subjects/{subject.id}/assignments/{asg.id}/scores"


async def test_save_scores_blocked_on_unconfirmed_name_link(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    link = await _link(db, subject, st, method=ClassroomLinkMethod.NAME, confirmed=False)
    await _work(db, asg, link)

    r = await teacher_client.post(
        _scores_url(subject, asg),
        data={"student_id": str(st.id), "score_report": "4"},
        follow_redirects=False,
    )

    assert r.status_code == 409
    assert "Спочатку підтвердіть" in r.text
    sa = await db.scalar(
        select(StudentAssignment).where(
            StudentAssignment.student_id == st.id,
            StudentAssignment.subjects_assignment_id == asg.id,
        )
    )
    assert sa is None or not sa.teacher_scores


async def test_save_scores_stamps_approval(teacher_client: AsyncClient, db, teacher, make_student):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(db, asg, await _link(db, subject, st))

    r = await teacher_client.post(
        _scores_url(subject, asg),
        data={"student_id": str(st.id), "score_report": "3"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    await db.refresh(grading)
    assert grading.approved_by == teacher.id
    assert grading.approved_at is not None
    logs = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "teacher_scores_set")))
        .scalars()
        .all()
    )
    assert len(logs) == 1
    assert logs[0].detail["llm_grading_id"] == grading.id
    assert logs[0].detail["edited_from_draft"] is True


async def test_save_scores_unedited_draft_is_not_edited(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(db, asg, await _link(db, subject, st))

    await teacher_client.post(
        _scores_url(subject, asg),
        data={"student_id": str(st.id), "score_report": "4"},
        follow_redirects=False,
    )

    log = (
        await db.execute(select(AuditLog).where(AuditLog.action == "teacher_scores_set"))
    ).scalar_one()
    assert log.detail["llm_grading_id"] == grading.id
    assert log.detail["edited_from_draft"] is False


async def test_new_version_after_approval_flags_needs_review(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    link = await _link(db, subject, st)
    now = datetime.now(UTC)
    await _work(db, asg, link, seen_at=now - timedelta(days=1))
    r = await teacher_client.post(
        _scores_url(subject, asg),
        data={"student_id": str(st.id), "score_report": "4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = await teacher_client.get(_board(subject, asg))
    assert "нова версія" not in page.text

    draft_v2 = {
        "criteria": {"report": {"points": 5, "justification": "j", "evidence": "e"}},
        "comment": "",
        "usage": {},
    }
    await _work(db, asg, link, seen_at=now, draft=draft_v2, content_hash="b")

    page = await teacher_client.get(_board(subject, asg))
    assert "нова версія" in page.text
    # The approved points stay; the teacher decides whether to take the new draft.
    assert 'value="4"' in _input(page.text, st.id, "report")
    sa = await db.scalar(
        select(StudentAssignment).where(
            StudentAssignment.student_id == st.id,
            StudentAssignment.subjects_assignment_id == asg.id,
        )
    )
    await db.refresh(sa)
    assert sa.teacher_scores == {"report": 4}


# ── File route ───────────────────────────────────────────────────────────────


class _FakeStorage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def download_bytes(self, key: str) -> bytes:
        self.keys.append(key)
        return b"PDF"


async def test_file_route_streams_and_checks_access(
    teacher_client: AsyncClient, db, teacher, make_student, make_user, monkeypatch
):
    storage = _FakeStorage()
    monkeypatch.setattr(teacher_classroom, "get_storage", lambda _s: storage)
    subject, asg, st = await _world(db, teacher, make_student)
    manifest = [
        {
            "drive_id": "d1",
            "name": "звіт 1.pdf",
            "mime": "application/pdf",
            "size": 3,
            "modified": "",
            "sha256": "x",
            "storage_key": "k/report.pdf",
            "skipped": None,
        },
        {
            "drive_id": "d2",
            "name": "huge.zip",
            "mime": "application/zip",
            "size": 0,
            "modified": "",
            "sha256": None,
            "storage_key": None,
            "skipped": "too_big",
        },
    ]
    work, _ = await _work(db, asg, await _link(db, subject, st), manifest=manifest)
    base = f"/teacher/subjects/{subject.id}/classroom/works/{work.id}/files"

    ok = await teacher_client.get(f"{base}/0")
    assert ok.status_code == 200
    assert ok.content == b"PDF"
    assert ok.headers["content-type"].startswith("application/pdf")
    assert (
        ok.headers["content-disposition"]
        == "attachment; filename*=UTF-8''%D0%B7%D0%B2%D1%96%D1%82%201.pdf"
    )
    assert storage.keys == ["k/report.pdf"]

    assert (await teacher_client.get(f"{base}/1")).status_code == 404
    assert (await teacher_client.get(f"{base}/2")).status_code == 404

    stranger = await make_user(role=UserRole.TEACHER, username="stranger")
    authenticate(teacher_client, stranger)
    denied = await teacher_client.get(f"{base}/0")
    assert denied.status_code in (403, 404)
    assert storage.keys == ["k/report.pdf"]
