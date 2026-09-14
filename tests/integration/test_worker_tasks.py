"""Integration tests for outbox worker task handlers.

Covers the four task modules dispatched by the outbox processor:
  - review_tasks.execute_ai_review_task   (AI / OpenAI path)
  - notification_tasks.*                   (email dispatcher + preference gating)
  - send_credentials_tasks.*               (credentials email)
  - check_tasks.execute_check_task         (docker check + state transitions)

All external I/O is mocked: the OpenAI client, the notification dispatcher, and
the docker/check-core execution. Tests use the real testcontainer Postgres via
the `db_session` fixture and drive the real outbox processor end-to-end so the
dispatch routing table is exercised, mirroring tests/integration/test_workers.py
and tests/integration/test_teacher_digest.py.
"""

from __future__ import annotations

import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.db.models.enums import (
    NotificationCase,
    NotificationMethod,
    OutboxEventType,
    OutboxMessageState,
    SubmissionStatus,
)
from submissions_checker.db.models.feedback_request import FeedbackRequest
from submissions_checker.db.models.feedback_token import FeedbackToken
from submissions_checker.db.models.group import Group
from submissions_checker.db.models.notification import Notification
from submissions_checker.db.models.notification_preference import NotificationPreference
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.db.models.semester import Semester
from submissions_checker.db.models.student import Student
from submissions_checker.db.models.student_assignment import StudentAssignment
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
from submissions_checker.db.models.submission import Submission
from submissions_checker.db.models.teacher_notification_queue import TeacherNotificationQueue
from submissions_checker.db.models.user import User
from submissions_checker.services import check_core
from submissions_checker.workers.scheduled import outbox_processor
from submissions_checker.workers.tasks import (
    check_tasks,
    notification_tasks,
    review_tasks,
    send_credentials_tasks,
)

# ── shared helpers ────────────────────────────────────────────────────────────


def _patch_processor_session(monkeypatch, db_session: AsyncSession) -> None:
    """Route the outbox processor's get_session() at the test's own session."""

    @asynccontextmanager
    async def fake_get_session():
        yield db_session

    monkeypatch.setattr(outbox_processor, "get_session", fake_get_session)


class _FakeDispatcher:
    """Stand-in for NotificationDispatcher that records sends instead of emailing."""

    def __init__(self, with_channel: bool = True) -> None:
        self._channels = [object()] if with_channel else []
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


def _patch_dispatcher(monkeypatch, module, dispatcher: _FakeDispatcher) -> None:
    """Replace build_dispatcher() in a task module with one returning *dispatcher*."""
    monkeypatch.setattr(module, "build_dispatcher", lambda _settings: dispatcher)


async def _process(db_session: AsyncSession, monkeypatch, message: OutboxMessage) -> OutboxMessage:
    """Persist *message*, run the real outbox processor, return the refreshed row."""
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    _patch_processor_session(monkeypatch, db_session)
    await outbox_processor.process_outbox_messages()
    await db_session.refresh(message)
    return message


async def _seed_submission(
    db: AsyncSession,
    suffix: str,
    *,
    status: SubmissionStatus = SubmissionStatus.PENDING,
    owner: bool = True,
    head_ref: str | None = None,
    repository_path: str | None = None,
    variant: str | None = None,
    source_metadata: dict | None = None,
) -> tuple[Student, User, SubjectsAssignment, StudentAssignment, Submission, Subject]:
    """Create the full student -> assignment -> submission chain for a test."""
    group = Group(name=f"grp-{suffix}")
    db.add(group)
    await db.flush()

    student = Student(
        group_id=group.id, email=f"stud-{suffix}@e.com", full_name=f"Stud {suffix}"
    )
    teacher = User(
        username=f"teach-{suffix}",
        password_hash="x",
        role="TEACHER",
        email=f"teach-{suffix}@e.com",
        is_active=True,
    )
    db.add_all([student, teacher])
    await db.flush()

    subject = Subject(name=f"Sub {suffix}", owner_id=teacher.id if owner else None)
    db.add(subject)
    await db.flush()

    sa_tmpl = SubjectsAssignment(
        subject_id=subject.id, title=f"Assignment {suffix}", code="lab1"
    )
    db.add(sa_tmpl)
    await db.flush()

    enrollment = StudentAssignment(
        student_id=student.id, subjects_assignment_id=sa_tmpl.id, variant=variant
    )
    db.add(enrollment)
    await db.flush()

    submission = Submission(
        students_assignment_id=enrollment.id,
        source_type="ZIP_UPLOAD",
        status=status,
        repository_path=repository_path,
        source_metadata=source_metadata or {},
    )
    db.add(submission)
    await db.flush()
    await db.commit()
    # NOTE: Submission has no `head_ref` column; review_tasks reads it via
    # getattr(..., None) for lab-id extraction, so we attach it as a transient
    # attribute when a test needs it rather than persisting it.
    if head_ref is not None:
        submission.head_ref = head_ref
    return student, teacher, sa_tmpl, enrollment, submission, subject


async def _make_student_user(db: AsyncSession, student: Student) -> User:
    """Attach a STUDENT-role user account to an already-seeded Student row —
    _seed_submission doesn't create one, but the in-app notification path
    resolves Notification.user_id via User.student_id."""
    user = User(
        username=f"stud-login-{student.id}",
        password_hash="x",
        role="STUDENT",
        student_id=student.id,
    )
    db.add(user)
    await db.commit()
    return user


# ══════════════════════════════════════════════════════════════════════════════
# 1. review_tasks — AI review path
# ══════════════════════════════════════════════════════════════════════════════


class _FakeChoice:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.message = SimpleNamespace(content=content)
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeOpenAI:
    """Records the create() call and returns a canned completion (or raises)."""

    def __init__(self, *, content: str | None = None, raises: Exception | None = None) -> None:
        self._content = content
        self._raises = raises
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._raises is not None:
                    raise outer._raises
                return _FakeCompletion(outer._content)

        self.chat = SimpleNamespace(completions=_Completions())


def _patch_openai(monkeypatch, fake: _FakeOpenAI) -> None:
    monkeypatch.setattr(review_tasks, "AsyncOpenAI", lambda *a, **k: fake)


async def _seed_ai_submission(db: AsyncSession, suffix: str):
    """Submission ready for AI review (status AWAITING_AI_REVIEW)."""
    student, teacher, sa, enr, sub, subject = await _seed_submission(
        db, suffix, status=SubmissionStatus.AWAITING_AI_REVIEW
    )
    return student, teacher, sa, enr, sub, subject


@pytest.mark.asyncio
async def test_ai_review_completed_path(db_session: AsyncSession, monkeypatch) -> None:
    """RUN_AI_REVIEW with next_step=completed -> COMPLETED + ai_review stored."""
    _, _, _, _, sub, _ = await _seed_ai_submission(db_session, "ai-done")

    fake = _FakeOpenAI(content='{"review": "Looks good"}')
    _patch_openai(monkeypatch, fake)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_AI_REVIEW,
        payload={"submission_id": sub.id, "next_step": "completed"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED
    assert sub.ai_review == {"review": "Looks good"}
    # AI client was called once with the configured model.
    assert len(fake.calls) == 1
    assert "messages" in fake.calls[0]


@pytest.mark.asyncio
async def test_ai_review_teacher_path_enqueues_review(
    db_session: AsyncSession, monkeypatch
) -> None:
    """RUN_AI_REVIEW with next_step=teacher -> AWAITING_TEACHER_REVIEW + queue row."""
    _, teacher, _, _, sub, _ = await _seed_ai_submission(db_session, "ai-teach")

    fake = _FakeOpenAI(content='```json\n{"review": "ok"}\n```')
    _patch_openai(monkeypatch, fake)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_AI_REVIEW,
        payload={"submission_id": sub.id, "next_step": "teacher"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW
    # Code-fenced JSON is unwrapped and parsed.
    assert sub.ai_review == {"review": "ok"}
    # A teacher-review queue row was enqueued for the subject owner.
    rows = (
        await db_session.execute(
            select(TeacherNotificationQueue).where(
                TeacherNotificationQueue.submission_id == sub.id,
                TeacherNotificationQueue.teacher_id == teacher.id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ai_review_empty_content_marks_failed(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Empty AI content -> submission AI_REVIEW_FAILED, message ERROR (raises)."""
    _, _, _, _, sub, _ = await _seed_ai_submission(db_session, "ai-empty")

    fake = _FakeOpenAI(content="")
    _patch_openai(monkeypatch, fake)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_AI_REVIEW,
        payload={"submission_id": sub.id, "next_step": "completed"},
    )
    message = await _process(db_session, monkeypatch, message)

    # The handler raised ValueError after transitioning -> processor marks ERROR.
    assert message.state == OutboxMessageState.ERROR
    assert message.retry_count == 1
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.AI_REVIEW_FAILED


@pytest.mark.asyncio
async def test_ai_review_client_error_is_handled(
    db_session: AsyncSession, monkeypatch
) -> None:
    """If the AI client raises, the message is marked ERROR for retry."""
    _, _, _, _, sub, _ = await _seed_ai_submission(db_session, "ai-raise")

    fake = _FakeOpenAI(raises=RuntimeError("openai down"))
    _patch_openai(monkeypatch, fake)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_AI_REVIEW,
        payload={"submission_id": sub.id, "next_step": "completed"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.ERROR
    assert "openai down" in (message.error_message or "")
    await db_session.refresh(sub)
    # Transition to AI_REVIEWING happened, but the client raised before completion.
    assert sub.status == SubmissionStatus.AI_REVIEWING


@pytest.mark.asyncio
async def test_ai_review_reads_repository_code(
    db_session: AsyncSession, monkeypatch, tmp_path
) -> None:
    """collect_lab_data is fed the repo dir; the prompt carries README + code."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Implement add()", encoding="utf-8")
    (repo / "main.py").write_text("def add(a, b): return a + b", encoding="utf-8")

    student, teacher, sa, enr, sub, subject = await _seed_submission(
        db_session,
        "ai-code",
        status=SubmissionStatus.AWAITING_AI_REVIEW,
        repository_path=str(repo),
    )

    fake = _FakeOpenAI(content='{"review": "ok"}')
    _patch_openai(monkeypatch, fake)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_AI_REVIEW,
        payload={"submission_id": sub.id, "next_step": "completed"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    user_msg = fake.calls[0]["messages"][-1]["content"]
    assert "Implement add()" in user_msg
    assert "def add" in user_msg


# ══════════════════════════════════════════════════════════════════════════════
# 2. notification_tasks
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_submission_reviewed_sends_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """SUBMISSION_REVIEWED dispatches an email to the student."""
    student, _, _, _, sub, _ = await _seed_submission(
        db_session, "rev-send", status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": sub.id, "action": "approve", "reason": "great"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert len(dispatcher.sent) == 1
    recipient, subject, body = dispatcher.sent[0]
    assert recipient == student.email
    assert "Assignment rev-send" in subject or "Assignment rev-send" in body


@pytest.mark.asyncio
async def test_submission_reviewed_suppressed_by_preference(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """A disabled SUBMISSION_CHECKED preference suppresses the email."""
    student, _, _, _, sub, _ = await _seed_submission(
        db_session, "rev-supp", status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    db_session.add(
        NotificationPreference(
            student_id=student.id,
            case=NotificationCase.SUBMISSION_CHECKED,
            method=NotificationMethod.EMAIL,
            enabled=False,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": sub.id, "action": "reject", "reason": "redo"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []  # suppressed, no email


@pytest.mark.asyncio
async def test_submission_reviewed_creates_in_app_notification_on_approve(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    student, _, _, _, sub, _ = await _seed_submission(
        db_session, "rev-inapp-ok", status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    user = await _make_student_user(db_session, student)
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    _patch_dispatcher(monkeypatch, notification_tasks, _FakeDispatcher())

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": sub.id, "action": "approve", "reason": ""},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    notif = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalar_one()
    assert "approved" in notif.title or "approved" in notif.body


@pytest.mark.asyncio
async def test_submission_reviewed_in_app_notification_survives_email_suppression(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """In-app notifications are a separate channel from email — the
    SUBMISSION_CHECKED/EMAIL preference must not suppress them."""
    student, _, _, _, sub, _ = await _seed_submission(
        db_session, "rev-inapp-supp", status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    user = await _make_student_user(db_session, student)
    db_session.add(
        NotificationPreference(
            student_id=student.id,
            case=NotificationCase.SUBMISSION_CHECKED,
            method=NotificationMethod.EMAIL,
            enabled=False,
        )
    )
    await db_session.commit()

    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": sub.id, "action": "reject", "reason": "redo"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []  # email suppressed
    notif = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalar_one()
    assert "redo" in notif.body  # in-app notification still created


@pytest.mark.asyncio
async def test_submission_reviewed_missing_submission_noops(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """A SUBMISSION_REVIEWED for an unknown submission finishes without sending."""
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SUBMISSION_REVIEWED,
        payload={"submission_id": 999999, "action": "approve"},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_quiz_result_sends_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """QUIZ_RESULT dispatches a quiz-score email to the student."""
    student, _, _, _, sub, _ = await _seed_submission(
        db_session, "quiz", status=SubmissionStatus.QUIZ_SENT
    )
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.QUIZ_RESULT,
        payload={
            "submission_id": sub.id,
            "score": 8,
            "max_score": 10,
            "is_passed": True,
            "attempts_left": 1,
            "attempt_id": 42,
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert len(dispatcher.sent) == 1
    assert dispatcher.sent[0][0] == student.email


@pytest.mark.asyncio
async def test_quiz_result_no_channel_skips(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """QUIZ_RESULT with no configured channel completes silently."""
    _, _, _, _, sub, _ = await _seed_submission(
        db_session, "quiz-noch", status=SubmissionStatus.QUIZ_SENT
    )
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher(with_channel=False)
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.QUIZ_RESULT,
        payload={
            "submission_id": sub.id,
            "score": 0,
            "max_score": 10,
            "is_passed": False,
            "attempt_id": 7,
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


async def _seed_feedback(db: AsyncSession, suffix: str, *, pref_enabled: bool | None = None):
    """Create student + feedback request + token; optionally a preference row."""
    group = Group(name=f"grp-{suffix}")
    db.add(group)
    await db.flush()
    student = Student(
        group_id=group.id, email=f"fb-{suffix}@e.com", full_name=f"FB {suffix}"
    )
    teacher = User(
        username=f"fbt-{suffix}", password_hash="x", role="TEACHER", is_active=True
    )
    db.add_all([student, teacher])
    await db.flush()
    subject = Subject(name=f"FbSub {suffix}", owner_id=teacher.id)
    semester = Semester(
        name=f"Sem {suffix}",
        season="FALL",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 1, 1),
    )
    db.add_all([subject, semester])
    await db.flush()
    fr = FeedbackRequest(
        subject_id=subject.id,
        semester_id=semester.id,
        created_by_teacher_id=teacher.id,
    )
    db.add(fr)
    await db.flush()
    token = FeedbackToken(
        feedback_request_id=fr.id, student_id=student.id, token=f"tok-{suffix}"
    )
    db.add(token)
    await db.flush()
    if pref_enabled is not None:
        db.add(
            NotificationPreference(
                student_id=student.id,
                case=NotificationCase.FEEDBACK_REQUEST,
                method=NotificationMethod.EMAIL,
                enabled=pref_enabled,
            )
        )
    await db.commit()
    return student, token


@pytest.mark.asyncio
async def test_feedback_request_sends_link(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """FEEDBACK_REQUEST_SENT emails the student their personal feedback link."""
    student, token = await _seed_feedback(db_session, "fb-ok")
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.FEEDBACK_REQUEST_SENT,
        payload={"feedback_token_id": token.id},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert len(dispatcher.sent) == 1
    recipient, _subject, body = dispatcher.sent[0]
    assert recipient == student.email
    assert token.token in body


@pytest.mark.asyncio
async def test_feedback_request_suppressed_by_preference(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """A disabled FEEDBACK_REQUEST preference suppresses the feedback email."""
    _, token = await _seed_feedback(db_session, "fb-supp", pref_enabled=False)
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.FEEDBACK_REQUEST_SENT,
        payload={"feedback_token_id": token.id},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_feedback_request_unknown_token_noops(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """FEEDBACK_REQUEST_SENT for a missing token finishes without sending."""
    monkeypatch.setattr(notification_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, notification_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.FEEDBACK_REQUEST_SENT,
        payload={"feedback_token_id": 123456},
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


@pytest.mark.asyncio
async def test_new_submission_event_is_noop(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The deprecated NEW_SUBMISSION handler dispatches harmlessly to FINISHED."""
    message = OutboxMessage(
        event_type=OutboxEventType.NEW_SUBMISSION,
        payload={"submission_id": 1},
    )
    message = await _process(db_session, monkeypatch, message)
    assert message.state == OutboxMessageState.FINISHED


# ══════════════════════════════════════════════════════════════════════════════
# 3. send_credentials_tasks
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_send_credentials_dispatches_email(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """SEND_CREDENTIALS emails the student username + password via the dispatcher."""
    monkeypatch.setattr(send_credentials_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher()
    _patch_dispatcher(monkeypatch, send_credentials_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SEND_CREDENTIALS,
        payload={
            "student_email": "newbie@e.com",
            "full_name": "New Bie",
            "username": "newbie",
            "password": "s3cret",
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert len(dispatcher.sent) == 1
    recipient, _subject, body = dispatcher.sent[0]
    assert recipient == "newbie@e.com"
    assert "newbie" in body and "s3cret" in body


@pytest.mark.asyncio
async def test_send_credentials_no_channel_is_silent(
    db_session: AsyncSession, test_settings, monkeypatch
) -> None:
    """With no email channel, SEND_CREDENTIALS completes without delivering."""
    monkeypatch.setattr(send_credentials_tasks, "get_settings", lambda: test_settings)
    dispatcher = _FakeDispatcher(with_channel=False)
    _patch_dispatcher(monkeypatch, send_credentials_tasks, dispatcher)

    message = OutboxMessage(
        event_type=OutboxEventType.SEND_CREDENTIALS,
        payload={
            "student_email": "nobody@e.com",
            "full_name": "No Body",
            "username": "nobody",
            "password": "pw",
        },
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    assert dispatcher.sent == []


# ══════════════════════════════════════════════════════════════════════════════
# 4. check_tasks — docker check execution (mocked)
# ══════════════════════════════════════════════════════════════════════════════


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("main.py", "print('hi')\n")


async def _seed_check_submission(
    db: AsyncSession,
    suffix: str,
    *,
    review_mode: str = "tests_only",
    saved_as: str = "x.zip",
    with_config: bool = True,
):
    """Submission in PENDING with a plugin config and a real ZIP on disk."""
    student, teacher, sa, enr, sub, subject = await _seed_submission(
        db,
        suffix,
        status=SubmissionStatus.PENDING,
        source_metadata={"saved_as": saved_as},
    )
    if with_config:
        cfg = SubjectPluginConfig(
            subject_id=subject.id,
            version=1,
            content_hash=f"h-{suffix}",
            config={
                "subjectCode": "sub",
                "assignments": {
                    "lab1": {
                        "review_mode": review_mode,
                        "sandbox": {"check_command": "check.py"},
                    }
                },
            },
        )
        db.add(cfg)
        await db.flush()
        await db.commit()
    return sub


def _patch_run_check(monkeypatch, outcome: check_core.CheckOutcome) -> None:
    async def fake_run_check(*, plan, submission_dir, plugin_dir, sandbox):
        return outcome

    monkeypatch.setattr(check_tasks.check_core, "run_check", fake_run_check)


@pytest.mark.asyncio
async def test_check_validation_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A validation_failed outcome -> VALIDATION_FAILED + check_reason recorded."""
    zip_path = tmp_path / "vf.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "vf", saved_as="vf.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("validation_failed", 0, 0, [], "bad files"),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert sub.test_results == {"check_reason": "bad files"}


@pytest.mark.asyncio
async def test_check_test_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A failed (low-score) outcome -> TEST_FAILED with results stored."""
    zip_path = tmp_path / "tf.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "tf", saved_as="tf.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome(
            "failed", 1, 2, [{"name": "t1", "passed": False}]
        ),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.TEST_FAILED
    assert sub.test_results["passed"] is False
    assert sub.test_results["score"] == 1
    assert sub.test_results["max_score"] == 2


@pytest.mark.asyncio
async def test_check_test_failed_notifies_student_in_app(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    zip_path = tmp_path / "tfn.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "tfn", saved_as="tfn.zip")
    sa_row = await db_session.get(StudentAssignment, sub.students_assignment_id)
    student = await db_session.get(Student, sa_row.student_id)
    user = await _make_student_user(db_session, student)

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("failed", 1, 2, [{"name": "t1", "passed": False}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    notif = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalar_one()
    assert "didn't pass" in notif.title or "pass" in notif.body.lower()


@pytest.mark.asyncio
async def test_check_passed_tests_only_completes(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A passed outcome in tests_only mode -> COMPLETED."""
    zip_path = tmp_path / "ok.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session, "ok", review_mode="tests_only", saved_as="ok.zip"
    )

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.COMPLETED
    assert sub.test_results["passed"] is True


@pytest.mark.asyncio
async def test_check_passed_tests_only_notifies_student_in_app(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    zip_path = tmp_path / "okn.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session, "okn", review_mode="tests_only", saved_as="okn.zip"
    )
    sa_row = await db_session.get(StudentAssignment, sub.students_assignment_id)
    student = await db_session.get(Student, sa_row.student_id)
    user = await _make_student_user(db_session, student)

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    notif = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == user.id)
        )
    ).scalar_one()
    assert "passed" in notif.title or "passed" in notif.body.lower()


@pytest.mark.asyncio
async def test_check_passed_tests_then_ai_enqueues_ai_review(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A passed outcome in tests_then_ai mode -> AWAITING_AI_REVIEW + RUN_AI_REVIEW."""
    zip_path = tmp_path / "ai.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session, "thenai", review_mode="tests_then_ai", saved_as="ai.zip"
    )
    sa_row = await db_session.get(StudentAssignment, sub.students_assignment_id)
    student = await db_session.get(Student, sa_row.student_id)
    user = await _make_student_user(db_session, student)

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_AI_REVIEW
    # A RUN_AI_REVIEW outbox message was enqueued for this submission.
    ai_msgs = (
        await db_session.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.RUN_AI_REVIEW
            )
        )
    ).scalars().all()
    assert any(m.payload.get("submission_id") == sub.id for m in ai_msgs)
    # Not final yet — no in-app notification at this intermediate step.
    notif_count = await db_session.scalar(
        select(func.count()).select_from(Notification).where(Notification.user_id == user.id)
    )
    assert notif_count == 0


@pytest.mark.asyncio
async def test_check_no_plugin_config_records_validation_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """No plugin config for the subject -> clean VALIDATION_FAILED.

    Regression guard for a fixed bug: the no-config branch (check_tasks.py)
    calls ``_fail_validation`` while the submission is still PENDING. The state
    machine only allows ``validation_failed`` from VALIDATING, so previously this
    raised InvalidTransitionError and the outbox message looped in ERROR/retry.
    ``_fail_validation`` now steps through ``start_validation`` first, so the
    submission converges on VALIDATION_FAILED with a teacher-facing reason and
    the message finishes.
    """
    sub = await _seed_check_submission(
        db_session, "nocfg", saved_as="nocfg.zip", with_config=False
    )
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert "No plugin configuration" in sub.test_results["check_reason"]


@pytest.mark.asyncio
async def test_check_bad_zip_records_validation_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A corrupt ZIP on disk -> clean VALIDATION_FAILED (same fixed bug path).

    The bad-ZIP early return also calls ``_fail_validation`` before
    ``start_validation``; it now records VALIDATION_FAILED instead of raising.
    """
    bad = tmp_path / "bad.zip"
    bad.write_text("not a zip", encoding="utf-8")
    sub = await _seed_check_submission(db_session, "bad", saved_as="bad.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert "Could not open submitted ZIP" in sub.test_results["check_reason"]


@pytest.mark.asyncio
async def test_check_missing_submission_noops(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """RUN_CHECKS for an unknown submission returns early -> FINISHED."""
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": 987654}
    )
    message = await _process(db_session, monkeypatch, message)
    assert message.state == OutboxMessageState.FINISHED


@pytest.mark.asyncio
async def test_check_passed_tests_then_teacher_awaits_review(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A passed outcome in tests_then_teacher mode -> AWAITING_TEACHER_REVIEW."""
    zip_path = tmp_path / "tt.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session, "thenteacher", review_mode="tests_then_teacher", saved_as="tt.zip"
    )

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_TEACHER_REVIEW


@pytest.mark.asyncio
async def test_check_passed_tests_then_ai_then_teacher_enqueues_ai_with_next_step(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """tests_then_ai_then_teacher mode -> AWAITING_AI_REVIEW + RUN_AI_REVIEW carrying next_step=teacher."""
    zip_path = tmp_path / "att.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session,
        "thenaiteacher",
        review_mode="tests_then_ai_then_teacher",
        saved_as="att.zip",
    )

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.AWAITING_AI_REVIEW
    ai_msgs = (
        await db_session.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.RUN_AI_REVIEW
            )
        )
    ).scalars().all()
    mine = [m for m in ai_msgs if m.payload.get("submission_id") == sub.id]
    assert mine and mine[0].payload.get("next_step") == "teacher"


@pytest.mark.asyncio
async def test_check_passed_tests_then_quiz_sends_quiz(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A passed outcome in tests_then_quiz mode -> QUIZ_SENT."""
    zip_path = tmp_path / "q.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(
        db_session, "thenquiz", review_mode="tests_then_quiz", saved_as="q.zip"
    )

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(
        monkeypatch,
        check_core.CheckOutcome("passed", 2, 2, [{"name": "t1", "passed": True}]),
    )

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.QUIZ_SENT


@pytest.mark.asyncio
async def test_check_misconfigured_plan_records_validation_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A ConfigError from plan resolution -> clean VALIDATION_FAILED with the reason."""
    zip_path = tmp_path / "cfg.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "badcfg", saved_as="cfg.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    def _bad_plan(config, assignment_code, variant, **_kwargs):
        return check_core.ConfigError("no check command configured for this assignment")

    monkeypatch.setattr(check_tasks.check_core, "resolve_check_plan", _bad_plan)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert "no check command" in sub.test_results["check_reason"]


@pytest.mark.asyncio
async def test_check_unsafe_archive_records_validation_failed(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A zip-slip / unsafe archive -> clean VALIDATION_FAILED, message FINISHED."""
    from submissions_checker.utils.safe_zip import UnsafeArchiveError

    zip_path = tmp_path / "unsafe.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "unsafe", saved_as="unsafe.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    def _boom(zf, dest):
        raise UnsafeArchiveError("path escapes extraction root")

    monkeypatch.setattr(check_tasks, "safe_extract", _boom)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert "unsafe" in sub.test_results["check_reason"].lower()


@pytest.mark.asyncio
async def test_check_missing_saved_as_raises_and_errors(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """A submission whose source_metadata lacks saved_as -> RuntimeError -> message ERROR."""
    sub = await _seed_check_submission(db_session, "nosaved", saved_as="nosaved.zip")
    sub.source_metadata = {}
    await db_session.commit()

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)

    message = OutboxMessage(
        event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id}
    )
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.ERROR
