"""Teacher-side squad assignment and the Операції block."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
)
from submissions_checker.db.models.squad import Squad

pytestmark = pytest.mark.asyncio


async def _arrange(db, teacher, make_student, *, max_size: int | None = 2):
    subject = Subject(name="Sq", owner_id=teacher.id, squad_max_size=max_size)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    asg = SubjectsAssignment(subject_id=subject.id, title="L1", code="l1", config={})
    db.add(asg)
    await db.commit()
    students = []
    for name in ("Anna A", "Bohdan B", "Chris C"):
        s = await make_student(full_name=name)
        db.add(SubjectsStudents(subject_id=subject.id, student_id=s.id))
        db.add(StudentAssignment(student_id=s.id, subjects_assignment_id=asg.id))
        students.append(s)
    await db.commit()
    return subject, students


async def test_assign_creates_locked_squad_and_lists_it(
    teacher_client: AsyncClient, teacher, db, make_student
) -> None:
    subject, (a, b, _) = await _arrange(db, teacher, make_student)
    page = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "Сформувати сквад" in page.text
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/squads/assign",
        data={"student_ids": [str(a.id), str(b.id)], "name": "Alpha"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "squad=assigned" in r.headers["location"]
    squad = (await db.execute(select(Squad))).scalar_one()
    assert squad.locked_at is not None and squad.name == "Alpha"
    page = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "Alpha" in page.text and "Anna A, Bohdan B" in page.text


async def test_assign_too_many_is_refused(
    teacher_client: AsyncClient, teacher, db, make_student
) -> None:
    subject, (a, b, c) = await _arrange(db, teacher, make_student)
    r = await teacher_client.post(
        f"/teacher/subjects/{subject.id}/squads/assign",
        data={"student_ids": [str(a.id), str(b.id), str(c.id)]},
        follow_redirects=False,
    )
    assert "squad_error=too_many" in r.headers["location"]
    assert (await db.execute(select(Squad))).scalars().all() == []


async def test_block_hidden_when_disabled(
    teacher_client: AsyncClient, teacher, db, make_student
) -> None:
    subject, _ = await _arrange(db, teacher, make_student, max_size=None)
    page = await teacher_client.get(f"/teacher/subjects/{subject.id}")
    assert "Сформувати сквад" not in page.text
