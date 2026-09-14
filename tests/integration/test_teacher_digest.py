"""Tests for teacher review notifications: enqueue routing + coalesced digest flush."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from submissions_checker.db.base import Base
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.db.models.user import User
from submissions_checker.services.notifications.templates import teacher_digest_template
from submissions_checker.workers.scheduled import teacher_digest_processor
from submissions_checker.workers.tasks import notification_tasks

# ── template (pure) ───────────────────────────────────────────────────────────


def test_digest_template_single_item() -> None:
    subject, body = teacher_digest_template(
        "Alice", [("Bob", "Lab 1", "http://x/1")], "http://x/teacher"
    )
    assert "1 submission awaiting" in subject
    assert "Bob" in body and "Lab 1" in body and "http://x/1" in body


def test_digest_template_lists_all_items() -> None:
    items = [(f"S{i}", f"Lab {i}", f"http://x/{i}") for i in range(3)]
    subject, body = teacher_digest_template("Alice", items, "http://x/teacher")
    assert "3 submissions awaiting" in subject
    for i in range(3):
        assert f"S{i}" in body and f"http://x/{i}" in body


# ── fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture
async def db_session(test_settings):
    """Function-scoped engine+session bound to the test's own event loop.

    Overrides the session-scoped conftest fixture, whose engine is created on a
    separate loop and triggers asyncpg 'another operation is in progress' when
    reused across per-test loops. Fresh schema per test also isolates committed rows.
    """
    engine = create_async_engine(str(test_settings.database_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.rollback()
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class _FakeDispatcher:
    def __init__(self, with_channel: bool = True) -> None:
        self._channels = [object()] if with_channel else []
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


async def _seed(
    db: AsyncSession, suffix: str, *, owner: bool = True, teacher_email: str | None = "set"
) -> tuple[User, Submission]:
    group = Group(name=f"grp-{suffix}")
    db.add(group)
    await db.flush()
    student = Student(group_id=group.id, email=f"stud-{suffix}@e.com", full_name=f"Stud {suffix}")
    email = f"teach-{suffix}@e.com" if teacher_email else None
    teacher = User(username=f"teach-{suffix}", password_hash="x", role="TEACHER", email=email)
    db.add_all([student, teacher])
    await db.flush()
    subject = Subject(name=f"Sub {suffix}", owner_id=teacher.id if owner else None)
    db.add(subject)
    await db.flush()
    sa_tmpl = SubjectsAssignment(subject_id=subject.id, title=f"Assignment {suffix}")
    db.add(sa_tmpl)
    await db.flush()
    enrollment = StudentAssignment(student_id=student.id, subjects_assignment_id=sa_tmpl.id)
    db.add(enrollment)
    await db.flush()
    submission = Submission(
        students_assignment_id=enrollment.id,
        source_type="ZIP_UPLOAD",
        status="AWAITING_TEACHER_REVIEW",
    )
    db.add(submission)
    await db.flush()
    return teacher, submission


async def _pending(db: AsyncSession, teacher_id: int) -> list[TeacherNotificationQueue]:
    rows = await db.execute(
        select(TeacherNotificationQueue).where(
            TeacherNotificationQueue.teacher_id == teacher_id,
            TeacherNotificationQueue.sent_at.is_(None),
        )
    )
    return list(rows.scalars().all())


# ── enqueue routing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_routes_to_subject_owner(db_session: AsyncSession) -> None:
    teacher, submission = await _seed(db_session, "owner")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)

    pending = await _pending(db_session, teacher.id)
    assert len(pending) == 1
    assert pending[0].submission_id == submission.id


@pytest.mark.asyncio
async def test_enqueue_falls_back_to_admins_when_ownerless(db_session: AsyncSession) -> None:
    admin = User(username="admin-fb", password_hash="x", role="ADMIN", email="admin@e.com")
    db_session.add(admin)
    _, submission = await _seed(db_session, "noowner", owner=False)
    await db_session.flush()

    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)

    pending = await _pending(db_session, admin.id)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(db_session: AsyncSession) -> None:
    teacher, submission = await _seed(db_session, "idem")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)

    pending = await _pending(db_session, teacher.id)
    assert len(pending) == 1


# ── flush / coalescing ────────────────────────────────────────────────────────


def _patch_flush(monkeypatch, db_session, settings, dispatcher) -> None:
    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(teacher_digest_processor, "get_session", fake_get_session)
    monkeypatch.setattr(teacher_digest_processor, "get_settings", lambda: settings)
    monkeypatch.setattr(teacher_digest_processor, "build_dispatcher", lambda _s: dispatcher)


@pytest.mark.asyncio
async def test_flush_coalesces_group_into_single_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    # Three submissions for one teacher (whole-group burst).
    teacher, sub1 = await _seed(db_session, "grpflush")
    settings = test_settings.model_copy(
        update={"teacher_digest_window_seconds": 0, "teacher_digest_max_batch": 1000}
    )
    # add two more submissions to the same teacher's subject
    enr_id = sub1.students_assignment_id
    for _ in range(2):
        s = Submission(
            students_assignment_id=enr_id,
            source_type="ZIP_UPLOAD",
            status="AWAITING_TEACHER_REVIEW",
        )
        db_session.add(s)
        await db_session.flush()
        await notification_tasks.enqueue_teacher_review_notification(db_session, s.id)
    await notification_tasks.enqueue_teacher_review_notification(db_session, sub1.id)

    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    mine = [s for s in dispatcher.sent if s[0] == teacher.email]
    assert len(mine) == 1  # one email, not three
    assert "3 submissions" in mine[0][1]
    assert await _pending(db_session, teacher.id) == []  # all marked sent


@pytest.mark.asyncio
async def test_flush_threshold_before_window(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    # window huge, threshold 1 -> eager flush despite window not elapsed
    teacher, submission = await _seed(db_session, "thresh")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(
        update={"teacher_digest_window_seconds": 99999, "teacher_digest_max_batch": 1}
    )
    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    assert any(s[0] == teacher.email for s in dispatcher.sent)
    assert await _pending(db_session, teacher.id) == []


@pytest.mark.asyncio
async def test_flush_not_resent(db_session: AsyncSession, test_settings, monkeypatch) -> None:
    teacher, submission = await _seed(db_session, "once")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(update={"teacher_digest_window_seconds": 0})
    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)

    await teacher_digest_processor.flush_teacher_digests()
    first = len([s for s in dispatcher.sent if s[0] == teacher.email])
    await teacher_digest_processor.flush_teacher_digests()
    second = len([s for s in dispatcher.sent if s[0] == teacher.email])
    assert first == 1 and second == 1  # no resend on the second flush


@pytest.mark.asyncio
async def test_flush_disabled_keeps_pending(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    teacher, submission = await _seed(db_session, "disabled")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(update={"teacher_digest_enabled": False})
    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    assert dispatcher.sent == []
    assert len(await _pending(db_session, teacher.id)) == 1


@pytest.mark.asyncio
async def test_flush_no_channel_keeps_pending(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    teacher, submission = await _seed(db_session, "nochan")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(update={"teacher_digest_window_seconds": 0})
    dispatcher = _FakeDispatcher(with_channel=False)
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    assert dispatcher.sent == []
    assert len(await _pending(db_session, teacher.id)) == 1


@pytest.mark.asyncio
async def test_flush_skips_teacher_without_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    teacher, submission = await _seed(db_session, "noemail", teacher_email=None)
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(update={"teacher_digest_window_seconds": 0})
    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    assert not any(s for s in dispatcher.sent if s[0] is None)
    assert len(await _pending(db_session, teacher.id)) == 1


@pytest.mark.asyncio
async def test_flush_not_ready_keeps_pending(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """Batch neither past the window nor at the threshold -> left pending, no email."""
    teacher, submission = await _seed(db_session, "notready")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    # huge window AND huge threshold -> a single fresh entry is never "ready"
    settings = test_settings.model_copy(
        update={"teacher_digest_window_seconds": 99999, "teacher_digest_max_batch": 1000}
    )
    dispatcher = _FakeDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    await teacher_digest_processor.flush_teacher_digests()

    assert dispatcher.sent == []
    assert len(await _pending(db_session, teacher.id)) == 1


@pytest.mark.asyncio
async def test_flush_send_failure_leaves_rows_pending(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """dispatcher.notify raising -> rows stay pending for retry, flush still completes."""
    teacher, submission = await _seed(db_session, "senderr")
    await notification_tasks.enqueue_teacher_review_notification(db_session, submission.id)
    settings = test_settings.model_copy(update={"teacher_digest_window_seconds": 0})

    class _BoomDispatcher(_FakeDispatcher):
        async def notify(self, recipient: str, subject: str, body: str) -> None:
            raise RuntimeError("smtp down")

    dispatcher = _BoomDispatcher()
    _patch_flush(monkeypatch, db_session, settings, dispatcher)
    # Must not raise — the send error is caught and the row left pending.
    await teacher_digest_processor.flush_teacher_digests()

    assert len(await _pending(db_session, teacher.id)) == 1


@pytest.mark.asyncio
async def test_flush_swallows_unexpected_error(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """An unexpected error inside the flush is logged and swallowed, not propagated."""
    settings = test_settings.model_copy(update={"teacher_digest_window_seconds": 0})

    @asynccontextmanager
    async def boom_get_session():
        raise RuntimeError("connection pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(teacher_digest_processor, "get_settings", lambda: settings)
    monkeypatch.setattr(teacher_digest_processor, "get_session", boom_get_session)

    # The outer try/except must absorb this — no exception escapes.
    await teacher_digest_processor.flush_teacher_digests()
