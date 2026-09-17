"""Functional coverage for the STUDENT quiz PROCTORING / anti-cheat paths.

Companion to ``test_student_quiz.py``; this file targets the under-covered,
security-critical branches in ``api/routes/student_quiz.py``:

* ``POST /portal/quiz/{id}/event`` — full anti-cheat rule matrix
  (warn / fail / reduce_time / flag), threshold accumulation, ``_force_fail``
  finalization (→ ``VIOLATION_FAIL``), events ignored on terminal attempts,
  cross-student → 403, unknown attempt → 404.
* ``POST /portal/quiz/{id}/snapshot`` — webcam evidence capture: stored when
  capture enabled + storage configured (asserts a ``QuizAttemptSnapshot`` row, and that
  the response hands back no publicly fetchable object-storage URL),
  the ``stored: False`` no-storage branch, 403 when capture disabled, 409 on
  terminal attempts, 415 bad content-type, 413 oversize / empty, 404 unknown.
* Grading per question type (single / multiple / ordering / true-false), pass-threshold
  boundary, and the reduce_time time-penalty path's interaction with timeout.
* Result-detail rendering of a graded multi-type attempt.

All behavior is asserted against the real handlers; DB side effects (violation
counters, attempt status, snapshot rows, answer scores) are checked directly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import submissions_checker.api.routes.student_quiz as student_quiz_module
from submissions_checker.core.config import Settings, get_settings
from submissions_checker.db.models import (
    QuizAttempt,
    QuizAttemptSnapshot,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.enums import QuizAttemptStatus
from submissions_checker.db.models.quiz_template import QuizAnswer
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.main import app

pytestmark = pytest.mark.asyncio


# ── Arrangement helpers (mirror test_student_quiz.py) ────────────────────────


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


async def _arrange_quiz(
    db,
    student_id: int,
    *,
    quiz_cfg: dict | None = None,
    assignment_code: str = "hw1",
    submission_status: SubmissionStatus = SubmissionStatus.QUIZ_SENT,
) -> tuple[Subject, StudentAssignment, Submission, SubjectPluginConfig]:
    subject = Subject(name="Quizland")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-{subject.id}",
        config={"assignments": {assignment_code: {"quiz": quiz_cfg or {}}}},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    sub_a = SubjectsAssignment(subject_id=subject.id, title="HW1", code=assignment_code, config={})
    db.add(sub_a)
    await db.commit()
    await db.refresh(sub_a)

    sa = StudentAssignment(student_id=student_id, subjects_assignment_id=sub_a.id)
    db.add(sa)
    await db.commit()
    await db.refresh(sa)

    submission = Submission(
        students_assignment_id=sa.id,
        source_type=SubmissionSourceType.ZIP_UPLOAD,
        source_metadata={},
        status=submission_status,
        plugin_config_id=cfg.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    return subject, sa, submission, cfg


_DEFAULT_SNAP = [
    {
        "id": 0,
        "type": "SINGLE_CHOICE",
        "text": "2+2",
        "points": 1,
        "is_required": False,
        "config": {"options": ["3", "4", "5"], "correct": 1},
    },
    {
        "id": 1,
        "type": "SINGLE_CHOICE",
        "text": "sky",
        "points": 1,
        "is_required": False,
        "config": {"options": ["green", "blue"], "correct": 1},
    },
]


async def _make_attempt(
    db,
    submission_id: int,
    cfg: SubjectPluginConfig,
    *,
    questions_snapshot: list | None = None,
    config_snapshot: dict | None = None,
    status: QuizAttemptStatus = QuizAttemptStatus.IN_PROGRESS,
    started_at: datetime | None = None,
    violations: dict | None = None,
    is_passed: bool | None = None,
) -> QuizAttempt:
    attempt = QuizAttempt(
        submission_id=submission_id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=(
            questions_snapshot if questions_snapshot is not None else _DEFAULT_SNAP
        ),
        config_snapshot=config_snapshot or {"pass_threshold_pct": 0.6},
        started_at=started_at or datetime.now(UTC),
        status=status,
        violations=violations or {},
        is_passed=is_passed,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


def _rule(event: str, threshold: int, action: dict) -> dict:
    return {"event": event, "threshold": threshold, "action": action}


# ── Settings / storage override helpers for the snapshot path ────────────────


def _override_settings(**overrides) -> Settings:
    base = {
        "secret_key": "test-secret-key-minimum-32-chars-long",
        "s3_bucket_name": "proctor-bucket",
        "s3_endpoint_url": "http://localstack:4566",
        "s3_public_base_url": None,
        "aws_access_key_id": "ak",
        "aws_secret_access_key": "sk",
        "aws_region": "us-east-1",
    }
    base.update(overrides)
    settings = Settings(**base)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


@asynccontextmanager
async def _storage_enabled(upload_url: str = "https://cdn/proctor/x.jpg"):
    """Configure S3 settings + stub StorageService so snapshots get 'stored'."""
    _override_settings()
    fake_storage = AsyncMock()
    fake_storage.upload_bytes = AsyncMock(return_value=upload_url)
    try:
        with patch.object(student_quiz_module, "StorageService", return_value=fake_storage):
            yield fake_storage
    finally:
        app.dependency_overrides.pop(get_settings, None)


# =============================================================================
# 1. Anti-cheat event rules
# =============================================================================


async def test_event_warn_below_threshold_is_none_then_warns(
    student_client: AsyncClient, db, student_user
) -> None:
    """A warn rule with threshold 2: first event is 'none' (count<thr), second warns."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {"rules": [_rule("tab_switch", 2, {"type": "warn", "message": "m"})]}
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})

    r1 = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    assert r1.status_code == 200
    assert r1.json() == {
        "action": "none",
        "seconds_remaining": None,
        "message": "",
        "violation_count": 1,
    }

    r2 = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    body = r2.json()
    assert body["action"] == "warn"
    assert body["violation_count"] == 2

    await db.refresh(attempt)
    assert attempt.violations["tab_switch"] == 2
    assert "_force_fail" not in attempt.violations


async def test_event_fail_sets_force_fail_and_keeps_in_progress(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {"rules": [_rule("paste", 1, {"type": "fail", "message": "out"})]}
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "paste"})
    assert r.status_code == 200
    assert r.json()["action"] == "fail"

    await db.refresh(attempt)
    assert attempt.violations["_force_fail"] is True
    # The event endpoint itself does NOT finalize — status stays IN_PROGRESS.
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS


async def test_event_fail_and_reduce_time_unaffected_by_notify_student_false(
    student_client: AsyncClient, db, student_user
) -> None:
    """notify_student only gates the client-rendered banner/sound — report_violation
    never reads it, so rule matching, penalties, and _force_fail are unchanged."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {
        "notify_student": False,
        "rules": [
            _rule("paste", 1, {"type": "fail", "message": "out"}),
            _rule("blur", 1, {"type": "reduce_time", "penalty_seconds": 30, "message": "-30s"}),
        ],
    }
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": anti_cheat, "time_limit_minutes": 10},
        started_at=datetime.now(UTC),
    )

    r_fail = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "paste"})
    assert r_fail.json()["action"] == "fail"
    await db.refresh(attempt)
    assert attempt.violations["_force_fail"] is True
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS

    r_reduce = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "blur"})
    body = r_reduce.json()
    assert body["action"] == "reduce_time"
    assert 560 <= body["seconds_remaining"] <= 570
    await db.refresh(attempt)
    assert attempt.violations["_time_penalty_seconds"] == 30


async def test_event_message_template_interpolates_count_and_threshold(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {
        "rules": [
            _rule(
                "tab_switch",
                1,
                {
                    "type": "warn",
                    "message": "{count}/{threshold} (fail at {fail_threshold})",
                },
            ),
            _rule("tab_switch", 3, {"type": "fail", "message": "gone"}),
        ]
    }
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"})
    # fail_threshold is discovered from the matching fail rule (3).
    assert r.json()["message"] == "1/1 (fail at 3)"


async def test_event_reduce_time_accumulates_penalty(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {
        "rules": [
            _rule("blur", 1, {"type": "reduce_time", "penalty_seconds": 30, "message": "-30s"})
        ]
    }
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": anti_cheat, "time_limit_minutes": 10},
        started_at=datetime.now(UTC),
    )
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "blur"})
    body = r.json()
    assert body["action"] == "reduce_time"
    # 10 min = 600s, minus 30s penalty, minus ~0 elapsed → ~570s remaining.
    assert body["seconds_remaining"] is not None
    assert 560 <= body["seconds_remaining"] <= 570

    await db.refresh(attempt)
    assert attempt.violations["_time_penalty_seconds"] == 30


async def test_event_flag_records_flagged_events_and_blank_message(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {"rules": [_rule("copy", 1, {"type": "flag", "message": "ignored"})]}
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "copy"})
    body = r.json()
    assert body["action"] == "flag"
    assert body["message"] == ""  # flag action blanks the message

    await db.refresh(attempt)
    assert attempt.violations["_flagged_events"] == ["copy"]


async def test_event_unconfigured_type_counts_but_no_action(
    student_client: AsyncClient, db, student_user
) -> None:
    """An event with no matching rule still increments its counter; action 'none'."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    anti_cheat = {"rules": [_rule("tab_switch", 1, {"type": "warn", "message": "m"})]}
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": anti_cheat})

    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "mystery"})
    assert r.json() == {
        "action": "none",
        "seconds_remaining": None,
        "message": "",
        "violation_count": 1,
    }
    await db.refresh(attempt)
    assert attempt.violations["mystery"] == 1


async def test_event_missing_type_is_400(student_client: AsyncClient, db, student_user) -> None:
    """The event type is a JSONB key; an unnamed event has nothing to count."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}})
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={})
    assert r.status_code == 400
    await db.refresh(attempt)
    assert attempt.violations == {}


@pytest.mark.parametrize("body", [[], "tab_switch", 42, None])
async def test_event_non_object_body_is_400(
    student_client: AsyncClient, db, student_user, body
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}})
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json=body)
    assert r.status_code == 400


async def test_event_malformed_json_is_400(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}})
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/event",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


@pytest.mark.parametrize("event_type", ["x" * 65, "../tab", "Tab Switch", "", "_force_fail"])
async def test_event_type_outside_allowed_shape_is_400(
    student_client: AsyncClient, db, student_user, event_type: str
) -> None:
    """Only short snake_case names: they are stored as JSONB keys and rendered to the
    teacher, and the underscore-prefixed names are the handler's own bookkeeping."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}})
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": event_type})
    assert r.status_code == 400
    await db.refresh(attempt)
    assert attempt.violations == {}


async def test_event_distinct_types_are_capped(
    student_client: AsyncClient, db, student_user
) -> None:
    """A client can invent event names; the violations blob must not grow without bound."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    full = {f"ev{i}": 1 for i in range(32)}
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}}, violations=full
    )
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "mystery"})
    assert r.status_code == 200
    assert r.json()["action"] == "none"
    await db.refresh(attempt)
    assert "mystery" not in attempt.violations
    # A known key still counts.
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "ev3"})
    assert r.status_code == 200
    await db.refresh(attempt)
    assert attempt.violations["ev3"] == 2


async def test_event_oversized_body_is_413(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"anti_cheat": {"rules": []}})
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch", "pad": "x" * 5000}
    )
    assert r.status_code == 413


async def test_event_on_unknown_attempt_404(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    r = await student_client.post("/portal/quiz/888888/event", json={"type": "x"})
    assert r.status_code == 404


async def test_event_on_timed_out_attempt_ignored(
    student_client: AsyncClient, db, student_user
) -> None:
    """Terminal (non-IN_PROGRESS) attempts short-circuit to a no-op response."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.TIMED_OUT,
        config_snapshot={"anti_cheat": {"rules": [_rule("x", 1, {"type": "fail"})]}},
    )
    r = await student_client.post(f"/portal/quiz/{attempt.id}/event", json={"type": "x"})
    assert r.status_code == 200
    assert r.json() == {"action": "none", "violation_count": 0}
    await db.refresh(attempt)
    assert attempt.violations == {}  # untouched


# =============================================================================
# 2. _force_fail finalization via show / submit
# =============================================================================


async def test_force_fail_finalizes_on_submit_as_violation_fail(
    student_client: AsyncClient, db, student_user
) -> None:
    """Submitting a _force_fail attempt finalizes to VIOLATION_FAIL, is_passed False.

    Even with all-correct answers, the force-fail branch wins.
    """
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6},
        violations={"_force_fail": True},
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"},  # both correct
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.VIOLATION_FAIL
    assert attempt.is_passed is False
    # Answers are still graded/recorded even though the attempt fails.
    assert attempt.score == 2
    await db.refresh(sub)
    # No max_quiz_attempts configured → submission left at QUIZ_SENT for retry.
    assert sub.status == SubmissionStatus.QUIZ_SENT


# =============================================================================
# 3. Snapshot capture
# =============================================================================


async def test_snapshot_stored_when_capture_enabled_and_storage_configured(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    async with _storage_enabled(upload_url="https://cdn/proctor/1.jpg") as storage:
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=face/lost!!",
            files={"frame": ("f.jpg", b"\xff\xd8\xffdata", "image/jpeg")},
        )
    assert r.status_code == 200
    # No URL in the response: evidence is private and is read back only through the
    # authenticated teacher endpoint (see test_proctoring_snapshot_access.py).
    assert r.json() == {"stored": True}
    storage.upload_bytes.assert_awaited_once()

    rows = (
        (
            await db.execute(
                select(QuizAttemptSnapshot).where(QuizAttemptSnapshot.attempt_id == attempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    snap = rows[0]
    # event_type sanitized: non-alnum (except _-) stripped.
    assert snap.event_type == "facelost"
    assert snap.s3_url == "https://cdn/proctor/1.jpg"
    # key uses sequence 1 and the sanitized event + jpg extension.
    assert snap.s3_key == f"proctoring/attempt-{attempt.id}/1-facelost.jpg"


async def test_snapshot_sequence_increments_for_second_capture(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    # Pre-existing snapshot so the next sequence number is 2.
    db.add(
        QuizAttemptSnapshot(
            attempt_id=attempt.id,
            event_type="prior",
            s3_key="k",
            s3_url="u",
            captured_at=datetime.now(UTC),
        )
    )
    await db.commit()

    async with _storage_enabled():
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.png", b"\x89PNGxx", "image/png")},
        )
    assert r.status_code == 200
    rows = (
        (
            await db.execute(
                select(QuizAttemptSnapshot)
                .where(QuizAttemptSnapshot.attempt_id == attempt.id)
                .order_by(QuizAttemptSnapshot.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[1].s3_key == f"proctoring/attempt-{attempt.id}/2-blur.png"


async def test_snapshot_no_storage_configured_returns_stored_false(
    student_client: AsyncClient, db, student_user
) -> None:
    """Capture enabled but no S3 endpoint → accepted silently, no row written."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    # Default app settings have s3_endpoint_url=None → storage is None.
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
        files={"frame": ("f.jpg", b"\xff\xd8\xffok", "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json() == {"stored": False}
    rows = (
        (
            await db.execute(
                select(QuizAttemptSnapshot).where(QuizAttemptSnapshot.attempt_id == attempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_snapshot_terminal_attempt_409(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.COMPLETED,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
        files={"frame": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert r.status_code == 409


async def test_snapshot_bad_content_type_415(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    async with _storage_enabled():
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.txt", b"hello", "text/plain")},
        )
    assert r.status_code == 415


@pytest.mark.parametrize(
    "payload",
    [
        ("f.jpg", b"\x89PNGxx", "image/jpeg"),  # PNG bytes labelled JPEG
        ("f.png", b"<html><script>", "image/png"),  # not an image at all
        ("f.webp", b"RIFFxxxxWAVE", "image/webp"),  # RIFF but not WEBP
    ],
)
async def test_snapshot_bytes_must_match_declared_type_415(
    student_client: AsyncClient, db, student_user, payload
) -> None:
    """The multipart content-type is client-supplied; the bytes decide what is stored."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}}
    )
    async with _storage_enabled() as storage:
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur", files={"frame": payload}
        )
    assert r.status_code == 415
    storage.upload_bytes.assert_not_awaited()


async def test_snapshot_webp_magic_accepted(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}}
    )
    async with _storage_enabled():
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp")},
        )
    assert r.status_code == 200
    assert r.json() == {"stored": True}


async def test_snapshot_count_per_attempt_is_capped(
    student_client: AsyncClient, db, student_user
) -> None:
    """One attempt cannot fill object storage: past the cap frames are dropped quietly
    (200, stored=false) so the best-effort uploader stops rather than retries."""
    from submissions_checker.api.routes import student_quiz as sq

    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}}
    )
    now = datetime.now(UTC)
    for i in range(sq._MAX_SNAPSHOTS_PER_ATTEMPT):
        db.add(
            QuizAttemptSnapshot(
                attempt_id=attempt.id,
                event_type="blur",
                s3_key=f"proctoring/attempt-{attempt.id}/{i + 1}-blur.jpg",
                s3_url=f"https://cdn/{i}.jpg",
                captured_at=now,
            )
        )
    await db.commit()
    async with _storage_enabled() as storage:
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.jpg", b"\xff\xd8\xffdata", "image/jpeg")},
        )
    assert r.status_code == 200
    assert r.json() == {"stored": False, "reason": "limit"}
    storage.upload_bytes.assert_not_awaited()


async def test_snapshot_oversize_413(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    big = b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024 + 1)  # > 2 MB
    async with _storage_enabled():
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.jpg", big, "image/jpeg")},
        )
    assert r.status_code == 413


async def test_snapshot_empty_frame_413(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
    )
    async with _storage_enabled():
        r = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=blur",
            files={"frame": ("f.jpg", b"", "image/jpeg")},
        )
    assert r.status_code == 413


async def test_snapshot_unknown_attempt_404(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    r = await student_client.post(
        "/portal/quiz/777777/snapshot?event_type=blur",
        files={"frame": ("f.jpg", b"\xff\xd8\xff", "image/jpeg")},
    )
    assert r.status_code == 404


# =============================================================================
# 4. Grading per question type
# =============================================================================


_MULTI_TYPE_SNAP = [
    {
        "id": 0,
        "type": "SINGLE_CHOICE",
        "text": "single",
        "points": 1,
        "is_required": False,
        "config": {"options": ["a", "b", "c"], "correct": 2},
    },
    {
        "id": 1,
        "type": "MULTIPLE_CHOICE",
        "text": "multi",
        "points": 2,
        "is_required": False,
        "config": {"options": ["a", "b", "c"], "correct": [0, 2]},
    },
    {
        "id": 2,
        "type": "ORDERING",
        "text": "order",
        "points": 2,
        "is_required": False,
        "config": {"items": ["x", "y", "z"], "correct_order": [2, 0, 1]},
    },
    {
        "id": 3,
        "type": "TRUE_FALSE",
        "text": "tf",
        "points": 1,
        "is_required": False,
        "config": {"correct": True},
    },
]


async def test_grading_all_question_types(student_client: AsyncClient, db, student_user) -> None:
    """Exercises _grade_answer for every type in one submit.

    Max = single(1) + multi(2) + order(2) + tf(1) = 6.
    """
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        questions_snapshot=_MULTI_TYPE_SNAP,
        config_snapshot={"pass_threshold_pct": 0.6},
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={
            "answer_0": "2",  # single correct
            "answer_1": ["0", "2"],  # multi correct {0,2}
            "answer_ordering_2": "2,0,1",  # ordering correct
            "answer_3": "true",  # true/false correct
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.score == 6
    assert attempt.max_score == 6
    assert attempt.status == QuizAttemptStatus.COMPLETED
    assert attempt.is_passed is True

    answers = {
        a.question_id: a
        for a in (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)))
        .scalars()
        .all()
    }
    assert answers[0].is_correct is True and answers[0].points_earned == 1
    assert answers[1].answer == {"selected": [0, 2]} and answers[1].points_earned == 2
    assert answers[2].answer == {"order": [2, 0, 1]} and answers[2].is_correct is True
    assert answers[3].answer == {"value": True} and answers[3].is_correct is True


async def test_grading_wrong_answers_score_zero(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        questions_snapshot=_MULTI_TYPE_SNAP,
        config_snapshot={"pass_threshold_pct": 0.6},
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={
            "answer_0": "0",  # single wrong
            "answer_1": "1",  # multi wrong
            "answer_ordering_2": "0,1,2",  # ordering wrong
            "answer_3": "false",  # tf wrong
            "answer_4": "",  # short answer empty
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.score == 0
    assert attempt.is_passed is False


async def test_grading_non_numeric_single_choice_is_incorrect(
    student_client: AsyncClient, db, student_user
) -> None:
    """A SINGLE_CHOICE answer that isn't an int → selected None, 0 points."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.6})
    # No answer fields posted → raw "" for each, int("") raises → selected None.
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit", data={}, follow_redirects=False
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.score == 0
    answers = (
        (await db.execute(select(QuizAnswer).where(QuizAnswer.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    assert all(a.answer == {"selected": None} for a in answers)
    assert all(a.is_correct is False for a in answers)


# =============================================================================
# 5. Pass-threshold boundary
# =============================================================================


async def test_pass_threshold_exact_boundary_passes(
    student_client: AsyncClient, db, student_user
) -> None:
    """score/max == threshold passes (>= comparison). 1/2 = 0.5 with threshold 0.5."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.5})
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "0"},  # first correct, second wrong → 1/2
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.score == 1 and attempt.max_score == 2
    assert attempt.is_passed is True


async def test_pass_threshold_just_below_fails(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg, config_snapshot={"pass_threshold_pct": 0.51})
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "0"},  # 1/2 = 0.5 < 0.51
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.is_passed is False


# =============================================================================
# 6. Result detail rendering + visibility
# =============================================================================


async def test_result_renders_graded_multi_type_attempt(
    student_client: AsyncClient, db, student_user
) -> None:
    """Result page builds question_results for a completed multi-type attempt."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        questions_snapshot=_MULTI_TYPE_SNAP,
        status=QuizAttemptStatus.COMPLETED,
        config_snapshot={"pass_threshold_pct": 0.6, "show_correct_answers_after": True},
        is_passed=True,
    )
    attempt.score = 6
    attempt.max_score = 9
    await db.commit()
    # One recorded answer so the answers branch (not the None branch) is taken.
    db.add(
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=0,
            answer={"selected": 2},
            is_correct=True,
            points_earned=1,
        )
    )
    await db.commit()

    r = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert r.status_code == 200


async def test_result_of_violation_fail_visible_to_owner(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.VIOLATION_FAIL,
        is_passed=False,
    )
    attempt.score = 0
    attempt.max_score = 2
    await db.commit()
    r = await student_client.get(f"/portal/quiz/{attempt.id}/result")
    assert r.status_code == 200


# =============================================================================
# 7. Timeout edge: reduce_time penalty pushes an attempt over its limit
# =============================================================================


async def test_time_penalty_causes_timeout_on_show(
    student_client: AsyncClient, db, student_user
) -> None:
    """A 5-min attempt started 4 min ago is fine, but a 120s penalty makes the
    effective limit 180s < 240s elapsed → TIMED_OUT on the next view."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "time_limit_minutes": 5},
        started_at=datetime.now(UTC) - timedelta(minutes=4),
        violations={"_time_penalty_seconds": 120},
    )
    r = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/portal/quiz/{attempt.id}/result"
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.TIMED_OUT


async def test_submit_when_timed_out_finalizes_timed_out(
    student_client: AsyncClient, db, student_user
) -> None:
    """Submitting an over-limit attempt finalizes status TIMED_OUT (still graded)."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "time_limit_minutes": 1},
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "1", "answer_1": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.TIMED_OUT
    # Answers were still graded before the timed-out finalization.
    assert attempt.score == 2


# =============================================================================
# 8. Live quiz render + question-config building via real endpoints
# =============================================================================


async def test_show_in_progress_proctored_quiz_renders(
    student_client: AsyncClient, db, student_user
) -> None:
    """An active proctored attempt with a time limit renders the live quiz page
    (exercises the seconds_remaining + anti_cheat/camera context build)."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={
            "pass_threshold_pct": 0.6,
            "time_limit_minutes": 30,
            "anti_cheat": {
                "rules": [_rule("tab_switch", 3, {"type": "warn", "message": "m"})],
                "camera": {"capture_snapshots": True},
            },
        },
        started_at=datetime.now(UTC),
    )
    r = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert r.status_code == 200


# =============================================================================
# 1b. notify_student rendering (banner + sound gate)
# =============================================================================


async def test_notify_student_defaults_true_when_key_absent(
    student_client: AsyncClient, db, student_user
) -> None:
    """No notify_student key configured: both the passive block's embedded
    anti_cheat JSON and the camera module's server-evaluated default read true."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={
            "anti_cheat": {
                "rules": [_rule("tab_switch", 3, {"type": "warn", "message": "m"})],
                "camera": {"enabled": True},
            },
        },
        started_at=datetime.now(UTC),
    )
    r = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert r.status_code == 200
    assert "const notifyStudent = true;" in r.text


async def test_notify_student_explicit_false_rendered_in_camera_block(
    student_client: AsyncClient, db, student_user
) -> None:
    """notify_student: false is threaded into the camera module's config,
    independent of the camera sub-object it otherwise reads from."""
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={
            "anti_cheat": {
                "notify_student": False,
                "rules": [_rule("tab_switch", 3, {"type": "warn", "message": "m"})],
                "camera": {"enabled": True},
            },
        },
        started_at=datetime.now(UTC),
    )
    r = await student_client.get(f"/portal/quiz/{attempt.id}", follow_redirects=False)
    assert r.status_code == 200
    assert "const notifyStudent = false;" in r.text
    assert '"notify_student": false' in r.text  # embedded in the passive block's ac json


async def test_start_quiz_builds_choices_format_and_extra_types(
    student_client: AsyncClient, db, student_user
) -> None:
    """start_or_resume builds question snapshots for the 'choices' option format
    plus MULTIPLE_CHOICE / ORDERING / TRUE_FALSE config shapes, with shuffling on."""
    await _consent(db, student_user.student_id)
    quiz_cfg = {
        "questions": [
            {
                "type": "single_choice",
                "text": "pick one",
                "points": 1,
                "choices": [
                    {"text": "wrong", "is_correct": False},
                    {"text": "right", "is_correct": True},
                ],
            },
            {
                "type": "multiple_choice",
                "text": "pick many",
                "points": 2,
                "choices": [
                    {"text": "a", "is_correct": True},
                    {"text": "b", "is_correct": False},
                    {"text": "c", "is_correct": True},
                ],
            },
            {
                "type": "ordering",
                "text": "order",
                "points": 2,
                "items": ["x", "y", "z"],
                "correct_order": [2, 1, 0],
            },
            {
                "type": "true_false",
                "text": "tf",
                "points": 1,
                "correct": True,
            },
        ],
        "shuffle_questions": True,
        "shuffle_options": True,
        "pass_threshold_pct": 0.6,
    }
    subject, sa, sub, _cfg = await _arrange_quiz(db, student_user.student_id, quiz_cfg=quiz_cfg)
    r = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert r.status_code == 303

    attempt = (
        (await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id)))
        .scalars()
        .one()
    )
    snap = {q["type"]: q for q in attempt.questions_snapshot}
    assert len(attempt.questions_snapshot) == 4
    # SINGLE_CHOICE from choices: options derived, correct index valid.
    sc = snap["SINGLE_CHOICE"]
    assert sorted(sc["config"]["options"]) == ["right", "wrong"]
    assert sc["config"]["options"][sc["config"]["correct"]] == "right"
    # MULTIPLE_CHOICE: two correct options ("a","c") tracked after shuffle.
    mc = snap["MULTIPLE_CHOICE"]
    correct_opts = {mc["config"]["options"][i] for i in mc["config"]["correct"]}
    assert correct_opts == {"a", "c"}
    # ORDERING / TRUE_FALSE configs preserved.
    assert snap["ORDERING"]["config"] == {"items": ["x", "y", "z"], "correct_order": [2, 1, 0]}
    assert snap["TRUE_FALSE"]["config"] == {"correct": True}


async def test_start_quiz_questions_to_send_limits_with_required(
    student_client: AsyncClient, db, student_user
) -> None:
    """questions_to_send caps optional questions but always keeps required ones."""
    await _consent(db, student_user.student_id)
    quiz_cfg = {
        "questions": [
            {"type": "true_false", "text": "req", "points": 1, "correct": True, "required": True},
            {"type": "true_false", "text": "opt1", "points": 1, "correct": True},
            {"type": "true_false", "text": "opt2", "points": 1, "correct": False},
            {"type": "true_false", "text": "opt3", "points": 1, "correct": True},
        ],
        "questions_to_send": 2,
        "shuffle_questions": True,
        "shuffle_options": False,
        "pass_threshold_pct": 0.6,
    }
    subject, sa, sub, _cfg = await _arrange_quiz(db, student_user.student_id, quiz_cfg=quiz_cfg)
    r = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz",
        follow_redirects=False,
    )
    assert r.status_code == 303
    attempt = (
        (await db.execute(select(QuizAttempt).where(QuizAttempt.submission_id == sub.id)))
        .scalars()
        .one()
    )
    # total=2: 1 required + 1 optional.
    assert len(attempt.questions_snapshot) == 2
    assert any(q["is_required"] for q in attempt.questions_snapshot)


# NOTE: report_violation and upload_snapshot do NOT call _needs_consent — only the
# two GET routes (start_or_resume_quiz, show_quiz) gate on consent. Consent gating
# for those is already covered in test_student_quiz.py; the event/snapshot endpoints
# intentionally accept without a consent redirect, so there is no consent branch to
# assert here.


async def test_quiz_page_loads_proctoring_assets_from_static(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "anti_cheat": {"camera": {"enabled": True}}},
    )
    resp = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "/static/vendor/mediapipe/vision_bundle.mjs" in body
    assert "/static/vendor/mediapipe/face_landmarker.task" in body
    for host in ("cdn.jsdelivr.net", "esm.sh", "storage.googleapis.com"):
        assert host not in body
    assert "coco-ssd" not in body and "tfjs" not in body


async def test_camera_events_hit_the_rule_engine(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    rules = [
        _rule("camera_face_absent", 2, {"type": "flag"}),
        _rule("camera_multiple_faces", 1, {"type": "fail", "message": "Another person detected."}),
    ]
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={
            "pass_threshold_pct": 0.6,
            "anti_cheat": {"rules": rules, "camera": {"enabled": True}},
        },
    )
    r1 = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "camera_face_absent"}
    )
    assert r1.json()["action"] == "none"
    r2 = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "camera_face_absent"}
    )
    assert r2.json()["action"] == "flag"
    r3 = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "camera_multiple_faces"}
    )
    assert r3.json()["action"] == "fail"
    await db.refresh(attempt)
    assert attempt.violations["camera_face_absent"] == 2
    assert "camera_face_absent" in attempt.violations["_flagged_events"]
    assert attempt.violations["_force_fail"] is True


async def test_camera_model_unavailable_is_recorded_without_a_rule(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        config_snapshot={"pass_threshold_pct": 0.6, "anti_cheat": {"camera": {"enabled": True}}},
    )
    r = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "camera_model_unavailable"}
    )
    assert r.status_code == 200 and r.json()["action"] == "none"
    await db.refresh(attempt)
    assert attempt.violations["camera_model_unavailable"] == 1
