"""Squads: every rule about forming one, and the one seam that resolves "the submission
for this student and assignment" across squad members.

Nothing here commits — callers own the transaction. Rule violations raise
``SquadError(reason)``; routes turn the reason into a vocab message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from submissions_checker.core.i18n import get_vocab
from submissions_checker.db.models import (
    QuizAttempt,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    User,
)
from submissions_checker.db.models.enums import EntityType, QuizAttemptStatus, SquadInviteStatus
from submissions_checker.db.models.squad import Squad, SquadInvite, SquadMember
from submissions_checker.services.audit import audit
from submissions_checker.services.notification_service import push_notification


class SquadError(Exception):
    def __init__(self, reason: str, student_id: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.student_id = student_id


@dataclass
class PendingState:
    squad: Squad | None = None  # unlocked squad the student created/joined
    outgoing: list[SquadInvite] = field(default_factory=list)
    incoming: list[SquadInvite] = field(default_factory=list)


@dataclass(frozen=True)
class MemberQuizState:
    student_id: int
    full_name: str
    submitted: bool
    passed: bool
    attempts_used: int


@dataclass(frozen=True)
class SquadShare:
    submission: Submission
    squad: Squad
    passed_student_ids: frozenset[int]


_TERMINAL = (
    QuizAttemptStatus.COMPLETED,
    QuizAttemptStatus.TIMED_OUT,
    QuizAttemptStatus.VIOLATION_FAIL,
)


def _now() -> datetime:
    return datetime.now(UTC)


def display_name(squad: Squad) -> str:
    return squad.name or f"Сквад #{squad.id}"


# ── lookups ───────────────────────────────────────────────────────────────────


async def max_size(db: AsyncSession, subject_id: int) -> int | None:
    return await db.scalar(select(Subject.squad_max_size).where(Subject.id == subject_id))


def _squad_query() -> Select[tuple[Squad]]:
    # populate_existing: callers (e.g. accept_invite) may already hold this Squad in the
    # identity map with a stale ``members`` collection loaded before a member was added —
    # selectinload alone skips re-fetching an already-populated relationship.
    return (
        select(Squad)
        .options(selectinload(Squad.members).selectinload(SquadMember.student))
        .execution_options(populate_existing=True)
    )


async def squad_of(
    db: AsyncSession, subject_id: int, student_id: int, *, locked_only: bool = False
) -> Squad | None:
    q = (
        _squad_query()
        .join(SquadMember, SquadMember.squad_id == Squad.id)
        .where(SquadMember.subject_id == subject_id, SquadMember.student_id == student_id)
    )
    if locked_only:
        q = q.where(Squad.locked_at.is_not(None))
    return (await db.execute(q)).scalar_one_or_none()


async def active_squad(db: AsyncSession, subject_id: int, student_id: int) -> Squad | None:
    return await squad_of(db, subject_id, student_id, locked_only=True)


async def list_squads(db: AsyncSession, subject_id: int) -> list[Squad]:
    result = await db.execute(
        _squad_query()
        .options(selectinload(Squad.invites))
        .where(Squad.subject_id == subject_id)
        .order_by(Squad.id)
    )
    return list(result.scalars().all())


async def _has_submissions(db: AsyncSession, subject_id: int, student_id: int) -> bool:
    count = await db.scalar(
        select(func.count(Submission.id))
        .join(StudentAssignment, StudentAssignment.id == Submission.students_assignment_id)
        .join(SubjectsAssignment, SubjectsAssignment.id == StudentAssignment.subjects_assignment_id)
        .where(
            StudentAssignment.student_id == student_id, SubjectsAssignment.subject_id == subject_id
        )
    )
    return bool(count)


async def eligibility(db: AsyncSession, subject_id: int, student_id: int) -> str | None:
    if await max_size(db, subject_id) is None:
        return "disabled"
    enrolled = await db.scalar(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == subject_id, SubjectsStudents.student_id == student_id
        )
    )
    if enrolled is None:
        return "not_enrolled"
    student_type = await db.scalar(select(Student.type).where(Student.id == student_id))
    if student_type == EntityType.TEST:
        return "test_student"
    if await squad_of(db, subject_id, student_id) is not None:
        return "already_in_squad"
    if await _has_submissions(db, subject_id, student_id):
        return "has_submissions"
    return None


async def eligible_classmates(db: AsyncSession, subject_id: int, student_id: int) -> list[Student]:
    result = await db.execute(
        select(Student)
        .join(SubjectsStudents, SubjectsStudents.student_id == Student.id)
        .where(
            SubjectsStudents.subject_id == subject_id,
            Student.id != student_id,
            Student.type == EntityType.REAL,
        )
        .order_by(Student.full_name)
    )
    out = []
    for s in result.scalars().all():
        if await eligibility(db, subject_id, s.id) is None:
            out.append(s)
    return out


async def has_pending_invites(db: AsyncSession, subject_id: int, student_id: int) -> bool:
    count = await db.scalar(
        select(func.count(SquadInvite.id))
        .join(Squad, Squad.id == SquadInvite.squad_id)
        .where(
            Squad.subject_id == subject_id,
            SquadInvite.status == SquadInviteStatus.PENDING,
            or_(
                SquadInvite.invited_student_id == student_id,
                SquadInvite.invited_by_student_id == student_id,
            ),
        )
    )
    return bool(count)


async def pending_state(db: AsyncSession, subject_id: int, student_id: int) -> PendingState:
    state = PendingState()
    squad = await squad_of(db, subject_id, student_id)
    if squad is not None and squad.locked_at is None:
        state.squad = squad
        state.outgoing = list(
            (
                await db.execute(
                    select(SquadInvite)
                    .options(selectinload(SquadInvite.invited_student))
                    .where(
                        SquadInvite.squad_id == squad.id,
                        SquadInvite.status == SquadInviteStatus.PENDING,
                    )
                )
            ).scalars()
        )
    state.incoming = list(
        (
            await db.execute(
                select(SquadInvite)
                .join(Squad, Squad.id == SquadInvite.squad_id)
                .options(
                    selectinload(SquadInvite.squad)
                    .selectinload(Squad.members)
                    .selectinload(SquadMember.student)
                )
                .where(
                    Squad.subject_id == subject_id,
                    SquadInvite.invited_student_id == student_id,
                    SquadInvite.status == SquadInviteStatus.PENDING,
                )
            )
        ).scalars()
    )
    return state


# ── notifications ─────────────────────────────────────────────────────────────


async def _notify_student(
    db: AsyncSession, student_id: int, key: str, subject_id: int, **fmt: object
) -> None:
    user_id = await db.scalar(select(User.id).where(User.student_id == student_id))
    if user_id is None:
        return
    vocab = get_vocab(None).get("squad", {})
    title = str(vocab.get(f"notif_{key}_title", key))
    body = str(vocab.get(f"notif_{key}_body", "")).format(**fmt)
    await push_notification(db, user_id, title, body, f"/portal/subjects/{subject_id}")


async def _member_names(db: AsyncSession, squad: Squad) -> str:
    names = [m.student.full_name for m in squad.members]
    return ", ".join(sorted(names))


# ── formation ─────────────────────────────────────────────────────────────────


async def _require_eligible(db: AsyncSession, subject_id: int, student_id: int) -> None:
    reason = await eligibility(db, subject_id, student_id)
    if reason is not None:
        raise SquadError(reason, student_id)


async def _cancel_pending_of(
    db: AsyncSession, subject_id: int, student_id: int, *, exclude_invite_id: int | None = None
) -> None:
    result = await db.execute(
        select(SquadInvite)
        .join(Squad, Squad.id == SquadInvite.squad_id)
        .where(
            Squad.subject_id == subject_id,
            SquadInvite.status == SquadInviteStatus.PENDING,
            or_(
                SquadInvite.invited_student_id == student_id,
                SquadInvite.invited_by_student_id == student_id,
            ),
        )
    )
    for inv in result.scalars():
        if exclude_invite_id is not None and inv.id == exclude_invite_id:
            continue
        inv.status = SquadInviteStatus.CANCELLED


async def _delete_if_empty(db: AsyncSession, squad: Squad) -> None:
    """Delete a squad left with one member, no PENDING invites and unlocked.

    The spec forbids lingering solo squads — this is shared by cancel_invite and
    decline_invite, the two ways a squad can drop back to one member.
    """
    still_pending = await db.scalar(
        select(func.count(SquadInvite.id)).where(
            SquadInvite.squad_id == squad.id, SquadInvite.status == SquadInviteStatus.PENDING
        )
    )
    if len(squad.members) <= 1 and not still_pending and squad.locked_at is None:
        await db.delete(squad)
        await db.flush()


async def create_with_invites(
    db: AsyncSession, subject_id: int, creator_id: int, invitee_ids: list[int]
) -> Squad:
    size = await max_size(db, subject_id)
    if size is None:
        raise SquadError("disabled")
    ids = list(dict.fromkeys(invitee_ids))
    if creator_id in ids:
        raise SquadError("self_invite")
    if not ids:
        raise SquadError("too_few")
    if len(ids) > size - 1:
        raise SquadError("too_many")
    await _require_eligible(db, subject_id, creator_id)
    for sid in ids:
        await _require_eligible(db, subject_id, sid)

    squad = Squad(subject_id=subject_id, created_by_student_id=creator_id)
    db.add(squad)
    await db.flush()
    db.add(
        SquadMember(
            squad_id=squad.id, student_id=creator_id, subject_id=subject_id, joined_at=_now()
        )
    )
    creator_name = await db.scalar(select(Student.full_name).where(Student.id == creator_id))
    for sid in ids:
        db.add(
            SquadInvite(squad_id=squad.id, invited_student_id=sid, invited_by_student_id=creator_id)
        )
        await _notify_student(db, sid, "invited", subject_id, name=creator_name)
    await audit(
        db,
        action="squad_create",
        target_type="squad",
        target_id=squad.id,
        subject_id=subject_id,
        creator_id=creator_id,
        invitees=ids,
    )
    await db.flush()
    return (await squad_of(db, subject_id, creator_id)) or squad


async def _load_invite(db: AsyncSession, invite_id: int) -> SquadInvite:
    inv = await db.get(
        SquadInvite,
        invite_id,
        options=[selectinload(SquadInvite.squad).selectinload(Squad.members)],
    )
    if inv is None or inv.status != SquadInviteStatus.PENDING:
        raise SquadError("not_pending")
    return inv


async def accept_invite(db: AsyncSession, invite_id: int, student_id: int) -> Squad:
    inv = await _load_invite(db, invite_id)
    if inv.invited_student_id != student_id:
        raise SquadError("not_yours")
    squad = inv.squad
    subject_id = squad.subject_id
    reason = await eligibility(db, subject_id, student_id)
    size = await max_size(db, subject_id) or 0
    if reason is None and squad.locked_at is not None:
        reason = "squad_locked"
    if reason is None and len(squad.members) >= size:
        reason = "squad_full"
    if reason is not None:
        inv.status = SquadInviteStatus.CANCELLED
        await db.flush()
        raise SquadError(reason, student_id)

    db.add(
        SquadMember(
            squad_id=squad.id, student_id=student_id, subject_id=subject_id, joined_at=_now()
        )
    )
    await db.flush()
    # Other pending invites of this student in the subject die with the join — but not
    # this one, which we mark ACCEPTED right after (the partial unique index on PENDING
    # means we must not flip it to CANCELLED and back).
    await _cancel_pending_of(db, subject_id, student_id, exclude_invite_id=inv.id)
    inv.status = SquadInviteStatus.ACCEPTED
    if len(squad.members) + 1 >= size:
        squad.locked_at = _now()
        for other in (
            await db.execute(
                select(SquadInvite).where(
                    SquadInvite.squad_id == squad.id,
                    SquadInvite.status == SquadInviteStatus.PENDING,
                )
            )
        ).scalars():
            other.status = SquadInviteStatus.CANCELLED
    await audit(
        db,
        action="squad_join",
        target_type="squad",
        target_id=squad.id,
        subject_id=subject_id,
        student_id=student_id,
    )
    if inv.invited_by_student_id is not None:
        joined_name = await db.scalar(select(Student.full_name).where(Student.id == student_id))
        await _notify_student(
            db, inv.invited_by_student_id, "accepted", subject_id, name=joined_name
        )
    await db.flush()
    refreshed = await squad_of(db, subject_id, student_id)
    assert refreshed is not None
    return refreshed


async def decline_invite(db: AsyncSession, invite_id: int, student_id: int) -> None:
    inv = await _load_invite(db, invite_id)
    if inv.invited_student_id != student_id:
        raise SquadError("not_yours")
    squad = inv.squad
    inv.status = SquadInviteStatus.DECLINED
    if inv.invited_by_student_id is not None:
        name = await db.scalar(select(Student.full_name).where(Student.id == student_id))
        await _notify_student(
            db, inv.invited_by_student_id, "declined", squad.subject_id, name=name
        )
    await db.flush()
    await _delete_if_empty(db, squad)


async def cancel_invite(db: AsyncSession, invite_id: int, student_id: int) -> None:
    inv = await _load_invite(db, invite_id)
    squad = inv.squad
    if squad.created_by_student_id != student_id:
        raise SquadError("not_yours")
    inv.status = SquadInviteStatus.CANCELLED
    await _notify_student(db, inv.invited_student_id, "cancelled", squad.subject_id)
    await db.flush()
    await _delete_if_empty(db, squad)


async def teacher_assign(
    db: AsyncSession,
    subject_id: int,
    teacher_user_id: int,
    student_ids: list[int],
    name: str | None = None,
) -> Squad:
    size = await max_size(db, subject_id)
    if size is None:
        raise SquadError("disabled")
    ids = list(dict.fromkeys(student_ids))
    if len(ids) < 2:
        raise SquadError("too_few")
    if len(ids) > size:
        raise SquadError("too_many")
    for sid in ids:
        await _require_eligible(db, subject_id, sid)
    squad = Squad(
        subject_id=subject_id,
        created_by_user_id=teacher_user_id,
        name=(name or "").strip() or None,
        locked_at=_now(),
    )
    db.add(squad)
    await db.flush()
    for sid in ids:
        await _cancel_pending_of(db, subject_id, sid)
        db.add(
            SquadMember(squad_id=squad.id, student_id=sid, subject_id=subject_id, joined_at=_now())
        )
    await db.flush()
    loaded = await squad_of(db, subject_id, ids[0])
    assert loaded is not None
    names = await _member_names(db, loaded)
    for sid in ids:
        await _notify_student(db, sid, "assigned", subject_id, members=names)
    await audit(
        db,
        action="squad_assign",
        actor_id=teacher_user_id,
        target_type="squad",
        target_id=squad.id,
        subject_id=subject_id,
        student_ids=ids,
    )
    return loaded


def lock_on_submit(squad: Squad) -> None:
    if squad.locked_at is None:
        squad.locked_at = _now()


# ── submission scope ──────────────────────────────────────────────────────────


async def member_sa_ids(db: AsyncSession, squad: Squad, subjects_assignment_id: int) -> list[int]:
    ids: list[int] = []
    for m in squad.members:
        sa = (
            await db.execute(
                select(StudentAssignment).where(
                    StudentAssignment.student_id == m.student_id,
                    StudentAssignment.subjects_assignment_id == subjects_assignment_id,
                )
            )
        ).scalar_one_or_none()
        if sa is None:
            sa = StudentAssignment(
                student_id=m.student_id, subjects_assignment_id=subjects_assignment_id
            )
            db.add(sa)
            await db.flush()
        ids.append(sa.id)
    return ids


async def scope_sa_ids(
    db: AsyncSession, student_id: int, subject_id: int, subjects_assignment_id: int
) -> list[int]:
    squad = await active_squad(db, subject_id, student_id)
    if squad is None:
        own = await db.scalar(
            select(StudentAssignment.id).where(
                StudentAssignment.student_id == student_id,
                StudentAssignment.subjects_assignment_id == subjects_assignment_id,
            )
        )
        return [own] if own is not None else []
    return await member_sa_ids(db, squad, subjects_assignment_id)


async def latest_submission_for(
    db: AsyncSession, student_id: int, subject_id: int, subjects_assignment_id: int
) -> Submission | None:
    scope = await scope_sa_ids(db, student_id, subject_id, subjects_assignment_id)
    if not scope:
        return None
    return (
        await db.execute(
            select(Submission)
            .where(Submission.students_assignment_id.in_(scope))
            .order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_submission(db: AsyncSession, sa: StudentAssignment) -> Submission | None:
    asg = await db.get(SubjectsAssignment, sa.subjects_assignment_id)
    if asg is None:
        return None
    return await latest_submission_for(db, sa.student_id, asg.subject_id, asg.id)


async def squad_for_submission(db: AsyncSession, submission: Submission) -> Squad | None:
    if submission.squad_id is None:
        return None
    return (
        await db.execute(_squad_query().where(Squad.id == submission.squad_id))
    ).scalar_one_or_none()


async def _passed_student_ids(db: AsyncSession, submission_id: int) -> set[int]:
    rows = await db.execute(
        select(QuizAttempt.student_id).where(
            QuizAttempt.submission_id == submission_id, QuizAttempt.is_passed.is_(True)
        )
    )
    return {sid for (sid,) in rows if sid is not None}


async def _enrolled_member_ids(db: AsyncSession, squad: Squad) -> set[int]:
    member_ids = {m.student_id for m in squad.members}
    rows = await db.execute(
        select(SubjectsStudents.student_id).where(
            SubjectsStudents.subject_id == squad.subject_id,
            SubjectsStudents.student_id.in_(member_ids),
        )
    )
    return {sid for (sid,) in rows}


async def quiz_complete(db: AsyncSession, submission: Submission) -> bool:
    squad = await squad_for_submission(db, submission)
    if squad is None:
        passed = await db.scalar(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.submission_id == submission.id, QuizAttempt.is_passed.is_(True)
            )
        )
        return bool(passed)
    needed = await _enrolled_member_ids(db, squad)
    return bool(needed) and needed <= await _passed_student_ids(db, submission.id)


async def member_quiz_states(db: AsyncSession, submission: Submission) -> list[MemberQuizState]:
    squad = await squad_for_submission(db, submission)
    if squad is None:
        return []
    submitter = await db.scalar(
        select(StudentAssignment.student_id).where(
            StudentAssignment.id == submission.students_assignment_id
        )
    )
    passed = await _passed_student_ids(db, submission.id)
    used_rows = await db.execute(
        select(QuizAttempt.student_id, func.count(QuizAttempt.id))
        .where(QuizAttempt.submission_id == submission.id, QuizAttempt.status.in_(_TERMINAL))
        .group_by(QuizAttempt.student_id)
    )
    used = {sid: n for sid, n in used_rows if sid is not None}
    return [
        MemberQuizState(
            student_id=m.student_id,
            full_name=m.student.full_name,
            submitted=m.student_id == submitter,
            passed=m.student_id in passed,
            attempts_used=used.get(m.student_id, 0),
        )
        for m in sorted(squad.members, key=lambda m: m.student.full_name)
    ]


async def shared_submissions(
    db: AsyncSession, subject_id: int, subjects_assignment_id: int | None = None
) -> dict[tuple[int, int], SquadShare]:
    """Latest squad submission per (squad, assignment), fanned out to every member."""
    latest_sq = (
        select(
            Submission.squad_id,
            StudentAssignment.subjects_assignment_id.label("asg_id"),
            func.max(Submission.created_at).label("max_at"),
        )
        .join(StudentAssignment, StudentAssignment.id == Submission.students_assignment_id)
        .where(Submission.squad_id.is_not(None))
        .group_by(Submission.squad_id, StudentAssignment.subjects_assignment_id)
        .subquery()
    )
    q = (
        select(Submission, StudentAssignment.subjects_assignment_id)
        .join(StudentAssignment, StudentAssignment.id == Submission.students_assignment_id)
        .join(
            latest_sq,
            and_(
                latest_sq.c.squad_id == Submission.squad_id,
                latest_sq.c.asg_id == StudentAssignment.subjects_assignment_id,
                latest_sq.c.max_at == Submission.created_at,
            ),
        )
        .join(Squad, Squad.id == Submission.squad_id)
        .where(Squad.subject_id == subject_id)
    )
    if subjects_assignment_id is not None:
        q = q.where(StudentAssignment.subjects_assignment_id == subjects_assignment_id)
    squads_by_id: dict[int, Squad] = {s.id: s for s in await list_squads(db, subject_id)}
    out: dict[tuple[int, int], SquadShare] = {}
    for submission, asg_id in (await db.execute(q)).all():
        squad = squads_by_id.get(submission.squad_id or -1)
        if squad is None:
            continue
        share = SquadShare(
            submission=submission,
            squad=squad,
            passed_student_ids=frozenset(await _passed_student_ids(db, submission.id)),
        )
        for m in squad.members:
            out[(m.student_id, asg_id)] = share
    return out
