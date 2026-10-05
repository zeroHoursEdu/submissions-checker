"""Student assignment page: the «received from Classroom» notice, never any LLM draft."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from submissions_checker.db.models import (
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
from submissions_checker.db.models.enums import ClassroomLinkMethod, LLMGradingStatus

pytestmark = pytest.mark.asyncio

NOTICE = "Роботу отримано з Google Classroom"
SECRET = "СЕКРЕТНЕ-ОБГРУНТУВАННЯ-ШІ"


async def _world(db, student_user):
    subject = Subject(name="S", owner_id=None)
    db.add(subject)
    await db.commit()
    asg = SubjectsAssignment(subject_id=subject.id, title="Lab", code="lab", max_grade=10)
    db.add(asg)
    await db.commit()
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student_user.student_id))
    sa = StudentAssignment(student_id=student_user.student_id, subjects_assignment_id=asg.id)
    db.add(sa)
    await db.commit()
    return subject, asg, sa


async def _link(db, subject, student_id, uid="u1", confirmed=True):
    link = ClassroomStudentLink(
        subject_id=subject.id,
        classroom_user_id=uid,
        classroom_name="ІП-43 Test Student",
        student_id=student_id,
        method=ClassroomLinkMethod.EMAIL.value,
        confirmed=confirmed,
    )
    db.add(link)
    await db.commit()
    return link


async def _work(db, asg, link, seen_at, key, *, draft=None, created_at=None):
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=link.id,
        classroom_submission_id=key,
        state="TURNED_IN",
        late=False,
        content_hash=key.ljust(64, "0"),
        manifest=[],
        seen_at=seen_at,
        **({"created_at": created_at} if created_at else {}),
    )
    db.add(work)
    await db.commit()
    db.add(
        LLMGrading(
            classroom_work_id=work.id,
            status=LLMGradingStatus.DONE.value,
            draft=draft,
        )
    )
    await db.commit()


def _url(subject, sa):
    return f"/portal/subjects/{subject.id}/assignments/{sa.id}"


async def test_student_sees_received_from_classroom(student_client: AsyncClient, db, student_user):
    subject, asg, sa = await _world(db, student_user)
    link = await _link(db, subject, student_user.student_id)
    await _work(
        db,
        asg,
        link,
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        "old",
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    await _work(
        db,
        asg,
        link,
        datetime(2026, 10, 3, 12, 30, tzinfo=UTC),
        "new",
        created_at=datetime(2026, 10, 3, 12, 30, tzinfo=UTC),
    )

    page = await student_client.get(_url(subject, sa))

    assert page.status_code == 200
    assert f"{NOTICE}: 03.10.2026" in page.text
    assert "01.10.2026" not in page.text


async def test_student_without_work_sees_no_notice(
    student_client: AsyncClient, db, student_user, make_student
):
    subject, asg, sa = await _world(db, student_user)
    other = await make_student(full_name="Інший")
    other_link = await _link(db, subject, other.id, uid="u2")
    await _work(db, asg, other_link, datetime(2026, 10, 3, tzinfo=UTC), "x")

    page = await student_client.get(_url(subject, sa))

    assert page.status_code == 200
    assert NOTICE not in page.text


async def test_student_never_sees_draft(student_client: AsyncClient, db, student_user):
    subject, asg, sa = await _world(db, student_user)
    link = await _link(db, subject, student_user.student_id)
    draft = {
        "criteria": {"report": {"points": 4, "justification": SECRET, "evidence": SECRET}},
        "comment": SECRET,
        "usage": {},
    }
    await _work(db, asg, link, datetime(2026, 10, 3, tzinfo=UTC), "w", draft=draft)

    page = await student_client.get(_url(subject, sa))

    assert NOTICE in page.text
    assert SECRET not in page.text
    assert "AI-чернетка" not in page.text


async def test_unconfirmed_name_link_gets_no_notice(student_client: AsyncClient, db, student_user):
    subject, asg, sa = await _world(db, student_user)
    link = await _link(db, subject, student_user.student_id, confirmed=False)
    await _work(db, asg, link, datetime(2026, 10, 3, tzinfo=UTC), "n")

    page = await student_client.get(_url(subject, sa))

    assert page.status_code == 200
    assert NOTICE not in page.text


async def test_notice_date_is_latest_versions_created_at_in_kyiv(
    student_client: AsyncClient, db, student_user
):
    subject, asg, sa = await _world(db, student_user)
    link = await _link(db, subject, student_user.student_id)
    # Latest version by seen_at; created 23:30 UTC on the 3rd = 02:30 on the 4th in Kyiv.
    await _work(
        db,
        asg,
        link,
        datetime(2026, 10, 9, tzinfo=UTC),
        "v2",
        created_at=datetime(2026, 10, 3, 23, 30, tzinfo=UTC),
    )
    await _work(
        db,
        asg,
        link,
        datetime(2026, 10, 1, tzinfo=UTC),
        "v1",
        created_at=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
    )

    page = await student_client.get(_url(subject, sa))

    assert f"{NOTICE}: 04.10.2026" in page.text
