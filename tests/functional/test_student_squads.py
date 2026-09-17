"""Student squad formation over the real app: invite → accept → locked; declines,
cancels, and the rules that refuse a join."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    User,
)
from submissions_checker.db.models.enums import SquadInviteStatus, UserRole
from submissions_checker.db.models.squad import Squad, SquadInvite
from submissions_checker.main import app
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


async def _subject(db, *, max_size: int | None = 2) -> tuple[Subject, SubjectsAssignment]:
    subject = Subject(name="Sq", squad_max_size=max_size)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    asg = SubjectsAssignment(subject_id=subject.id, title="L1", code="l1", config={})
    db.add(asg)
    await db.commit()
    await db.refresh(asg)
    return subject, asg


async def _enrol(db, subject: Subject, asg: SubjectsAssignment, student: Student) -> None:
    db.add(SubjectsStudents(subject_id=subject.id, student_id=student.id))
    db.add(StudentAssignment(student_id=student.id, subjects_assignment_id=asg.id))
    await db.commit()


async def _pair(db, make_user, make_student, subject, asg) -> tuple[User, User]:
    sa = await make_student(full_name="Anna A")
    sb = await make_student(full_name="Bohdan B")
    ua = await make_user(role=UserRole.STUDENT, username="anna", student=sa)
    ub = await make_user(role=UserRole.STUDENT, username="bohdan", student=sb)
    await _enrol(db, subject, asg, sa)
    await _enrol(db, subject, asg, sb)
    return ua, ub


def _client(user: User) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    authenticate(c, user)
    return c


async def test_invite_accept_locks_pair(db, make_user, make_student) -> None:
    subject, asg = await _subject(db)
    ua, ub = await _pair(db, make_user, make_student, subject, asg)
    async with _client(ua) as ca, _client(ub) as cb:
        page = await ca.get(f"/portal/subjects/{subject.id}")
        assert "Запросити у сквад" in page.text and "Bohdan B" in page.text

        r = await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
            follow_redirects=False,
        )
        assert r.status_code == 303

        page_b = await cb.get(f"/portal/subjects/{subject.id}")
        assert "запрошує вас у сквад" in page_b.text
        inv = (await db.execute(select(SquadInvite))).scalar_one()
        r = await cb.post(
            f"/portal/subjects/{subject.id}/squad/invites/{inv.id}/accept", follow_redirects=False
        )
        assert r.status_code == 303

        squad = (await db.execute(select(Squad))).scalar_one()
        await db.refresh(squad)
        assert squad.locked_at is not None
        page_a = await ca.get(f"/portal/subjects/{subject.id}")
        assert "Склад сквaду зафіксовано" in page_a.text
        assert "Запросити у сквад" not in page_a.text


async def test_only_invitee_can_accept(db, make_user, make_student) -> None:
    subject, asg = await _subject(db)
    ua, ub = await _pair(db, make_user, make_student, subject, asg)
    async with _client(ua) as ca:
        await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
        )
        inv = (await db.execute(select(SquadInvite))).scalar_one()
        r = await ca.post(
            f"/portal/subjects/{subject.id}/squad/invites/{inv.id}/accept", follow_redirects=False
        )
        assert r.status_code == 303 and "squad_error=not_yours" in r.headers["location"]


async def test_decline_and_cancel(db, make_user, make_student) -> None:
    subject, asg = await _subject(db)
    ua, ub = await _pair(db, make_user, make_student, subject, asg)
    async with _client(ua) as ca, _client(ub) as cb:
        await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
        )
        inv = (await db.execute(select(SquadInvite))).scalar_one()
        r = await cb.post(
            f"/portal/subjects/{subject.id}/squad/invites/{inv.id}/decline", follow_redirects=False
        )
        # Declining leaves the squad with one member and no pending invites, so
        # squads.decline_invite deletes it via _delete_if_empty — and the FK cascade
        # (Squad.invites is cascade="all, delete-orphan" / ondelete="CASCADE") takes the
        # just-declined invite row with it. There is nothing left to refresh; the redirect
        # and the empty tables are the observable outcome.
        assert r.status_code == 303 and "squad_flash=declined" in r.headers["location"]
        assert (await db.execute(select(SquadInvite))).scalars().all() == []
        assert (await db.execute(select(Squad))).scalars().all() == []

        await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
        )
        inv2 = (
            await db.execute(
                select(SquadInvite).where(SquadInvite.status == SquadInviteStatus.PENDING)
            )
        ).scalar_one()
        await ca.post(f"/portal/subjects/{subject.id}/squad/invites/{inv2.id}/cancel")
        assert (await db.execute(select(Squad))).scalars().all() == []


async def test_pending_card_and_error_banner_render(db, make_user, make_student) -> None:
    subject, asg = await _subject(db)
    ua, ub = await _pair(db, make_user, make_student, subject, asg)
    async with _client(ua) as ca:
        page = await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
            follow_redirects=True,
        )
        assert "Очікуємо відповіді:" in page.text
        assert "Bohdan B" in page.text
        assert "Скасувати запрошення" in page.text
        assert "Здача заблокована" in page.text

        page2 = await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": [str(ub.student_id)]},
            follow_redirects=True,
        )
        assert "Ви вже у сквaді" in page2.text


async def test_card_absent_when_subject_has_no_squads(db, make_user, make_student) -> None:
    subject, asg = await _subject(db, max_size=None)
    ua, _ = await _pair(db, make_user, make_student, subject, asg)
    async with _client(ua) as ca:
        page = await ca.get(f"/portal/subjects/{subject.id}")
        assert "Запросити у сквад" not in page.text
        r = await ca.post(
            f"/portal/subjects/{subject.id}/squad/create",
            data={"invitee_ids": ["1"]},
            follow_redirects=False,
        )
        assert "squad_error=disabled" in r.headers["location"]
