"""Functional coverage for pausing a quiz during an air raid.

The alert source is replaced with a fake through the app's dependency override, so no test
touches the network. The behaviour that matters most is negative: a pause must not be
granted on an unverified claim, and while paused the page must not leak the questions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.api.dependencies import get_air_raid_provider
from submissions_checker.db.models import (
    QuizAttempt,
    QuizAttemptPause,
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
from submissions_checker.services.air_raid.fake import FakeAirRaidProvider

pytestmark = pytest.mark.asyncio

# Kharkiv — oblast uid 22 in the alerts.in.ua table.
KHARKIV = {"lat": 49.9935, "lng": 36.2304}
KHARKIV_UID = 22
# Warsaw: outside the Ukrainian alert system entirely.
WARSAW = {"lat": 52.2297, "lng": 21.0122}

QUESTION_TEXT = "Which planet is closest to the Sun?"

SNAPSHOT = [
    {
        "id": 0,
        "type": "SINGLE_CHOICE",
        "text": QUESTION_TEXT,
        "points": 1,
        "is_required": False,
        "time_limit_seconds": None,
        "config": {"options": ["Venus", "Mercury"], "correct": 1},
    },
    {
        "id": 1,
        "type": "SINGLE_CHOICE",
        "text": "Second question text",
        "points": 1,
        "is_required": False,
        "time_limit_seconds": None,
        "config": {"options": ["a", "b"], "correct": 1},
    },
]

STEPPED_SNAPSHOT = [dict(q, time_limit_seconds=60) for q in SNAPSHOT]


@pytest.fixture
def air_raid_over(request):
    """Override the alert provider. Parametrise with the set of alerting region uids."""
    provider = FakeAirRaidProvider(active_uids=getattr(request, "param", {KHARKIV_UID}))
    app.dependency_overrides[get_air_raid_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_air_raid_provider, None)


@pytest.fixture
def no_alert_anywhere():
    provider = FakeAirRaidProvider(active_uids=set())
    app.dependency_overrides[get_air_raid_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_air_raid_provider, None)


@pytest.fixture
def provider_down():
    provider = FakeAirRaidProvider(active_uids={KHARKIV_UID}, fail=True)
    app.dependency_overrides[get_air_raid_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_air_raid_provider, None)


@pytest.fixture
def provider_unconfigured():
    app.dependency_overrides[get_air_raid_provider] = lambda: None
    yield
    app.dependency_overrides.pop(get_air_raid_provider, None)


# ── Arrangement ──────────────────────────────────────────────────────────────


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


async def _arrange(db, student_id: int) -> tuple[Submission, SubjectPluginConfig]:
    subject = Subject(name=f"Airraidland-{student_id}")
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-{subject.id}",
        config={"assignments": {"hw1": {"quiz": {"questions": []}}}},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)

    sub_a = SubjectsAssignment(subject_id=subject.id, title="HW1", code="hw1", config={})
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
        status=SubmissionStatus.QUIZ_SENT,
        plugin_config_id=cfg.id,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    return submission, cfg


async def _attempt(
    db,
    submission_id: int,
    cfg: SubjectPluginConfig,
    *,
    stepped: bool = False,
    time_limit_minutes: int | None = 30,
    started_ago: float = 60,
    paused_at: datetime | None = None,
    paused_seconds: int = 0,
    status: QuizAttemptStatus = QuizAttemptStatus.IN_PROGRESS,
    anti_cheat: dict | None = None,
) -> QuizAttempt:
    config: dict = {"pass_threshold_pct": 0.6}
    if time_limit_minutes is not None:
        config["time_limit_minutes"] = time_limit_minutes
    if stepped:
        config["per_question_timing"] = True
    if anti_cheat is not None:
        config["anti_cheat"] = anti_cheat

    now = datetime.now(UTC)
    attempt = QuizAttempt(
        submission_id=submission_id,
        plugin_config_id=cfg.id,
        plugin_config_version=cfg.version,
        questions_snapshot=STEPPED_SNAPSHOT if stepped else SNAPSHOT,
        config_snapshot=config,
        started_at=now - timedelta(seconds=started_ago),
        question_started_at=(now - timedelta(seconds=20)) if stepped else None,
        paused_at=paused_at,
        paused_seconds=paused_seconds,
        status=status,
        violations={},
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


# ── Granting a pause ─────────────────────────────────────────────────────────


async def test_active_alert_pauses_the_attempt(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    await db.refresh(attempt)
    assert attempt.paused_at is not None
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS

    pause = (await db.execute(select(QuizAttemptPause))).scalar_one()
    assert pause.attempt_id == attempt.id
    assert pause.ended_at is None
    assert pause.region_uid == KHARKIV_UID
    # The coordinates are kept so a suspicious pause can be audited afterwards.
    assert float(pause.latitude) == pytest.approx(KHARKIV["lat"], abs=1e-5)
    assert float(pause.longitude) == pytest.approx(KHARKIV["lng"], abs=1e-5)
    assert air_raid_over.calls == [KHARKIV_UID]


async def test_pausing_twice_is_idempotent(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    first = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    second = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert first.json()["paused"] is True
    assert second.json()["paused"] is True

    pauses = (await db.execute(select(QuizAttemptPause))).scalars().all()
    assert len(pauses) == 1


# ── Refusing a pause ─────────────────────────────────────────────────────────


async def test_no_active_alert_does_not_pause(
    student_client: AsyncClient, db, student_user, no_alert_anywhere
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.status_code == 200
    assert resp.json() == {"paused": False, "reason": "no_alert", "region": "Харківська область"}

    await db.refresh(attempt)
    assert attempt.paused_at is None
    assert (await db.execute(select(QuizAttemptPause))).first() is None


async def test_coordinates_outside_ukraine_do_not_pause(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=WARSAW)
    assert resp.json() == {"paused": False, "reason": "outside_coverage"}

    await db.refresh(attempt)
    assert attempt.paused_at is None
    # The provider is never even consulted for a location it cannot describe.
    assert air_raid_over.calls == []


async def test_a_provider_outage_does_not_pause(
    student_client: AsyncClient, db, student_user, provider_down
) -> None:
    """An unverifiable claim must not stop a graded clock — nor break the quiz."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.status_code == 200
    assert resp.json() == {"paused": False, "reason": "unavailable"}

    await db.refresh(attempt)
    assert attempt.paused_at is None


async def test_an_unconfigured_provider_does_not_pause(
    student_client: AsyncClient, db, student_user, provider_unconfigured
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.json() == {"paused": False, "reason": "unavailable"}
    await db.refresh(attempt)
    assert attempt.paused_at is None


async def test_missing_coordinates_are_rejected(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    """A denied geolocation never posts; a request without coordinates is malformed."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json={})
    assert resp.status_code == 400
    await db.refresh(attempt)
    assert attempt.paused_at is None


async def test_an_expired_attempt_is_settled_rather_than_paused(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    """Pausing at 0:00 must not freeze a window that has in fact already closed."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, time_limit_minutes=10, started_ago=1200)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.json() == {"paused": False, "reason": "attempt_closed"}

    await db.refresh(attempt)
    assert attempt.paused_at is None
    assert attempt.status == QuizAttemptStatus.TIMED_OUT


async def test_a_finished_attempt_cannot_be_paused(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.json() == {"paused": False, "reason": "attempt_closed"}


async def test_another_students_attempt_cannot_be_paused(
    student_client: AsyncClient, db, student_user, make_student, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    sub, cfg = await _arrange(db, other.id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    assert resp.status_code == 403
    await db.refresh(attempt)
    assert attempt.paused_at is None


# ── While paused ─────────────────────────────────────────────────────────────


async def test_the_paused_page_does_not_contain_the_question(
    student_client: AsyncClient, db, student_user
) -> None:
    """Pausing must not become a way to read a question with the clock stopped."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, paused_at=datetime.now(UTC))

    resp = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert resp.status_code == 200
    assert QUESTION_TEXT not in resp.text
    assert "Second question text" not in resp.text
    assert "Mercury" not in resp.text
    # No answer form, and no anti-cheat machinery either.
    assert 'id="quiz-form"' not in resp.text
    assert "proctor-video" not in resp.text
    # The resume control is there.
    assert f"/portal/quiz/{attempt.id}/airraid/resume" in resp.text


async def test_reopening_a_paused_attempt_shows_the_same_frozen_timer(
    student_client: AsyncClient, db, student_user
) -> None:
    """Requirement: close the tab, come back later, same place and same timer value."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    # Paused an hour ago, after 60s of quiz time, against a 30-minute limit.
    attempt = await _attempt(
        db,
        sub.id,
        cfg,
        time_limit_minutes=30,
        started_ago=60 + 3600,
        paused_at=datetime.now(UTC) - timedelta(seconds=3600),
    )

    first = await student_client.get(f"/portal/quiz/{attempt.id}")
    second = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert first.status_code == second.status_code == 200
    # 29:00 left, and it has not moved despite an hour of wall clock.
    assert "29:00" in first.text
    assert "29:00" in second.text

    await db.refresh(attempt)
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS


async def test_violation_events_are_ignored_while_paused(
    student_client: AsyncClient, db, student_user
) -> None:
    """A student running to a shelter must not fail for leaving the tab."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(
        db,
        sub.id,
        cfg,
        paused_at=datetime.now(UTC),
        anti_cheat={"rules": [{"event": "tab_switch", "threshold": 1, "action": {"type": "fail"}}]},
    )

    for _ in range(5):
        resp = await student_client.post(
            f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "none"
        assert resp.json()["paused"] is True

    await db.refresh(attempt)
    assert attempt.violations == {}
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS


async def test_snapshot_uploads_are_ignored_while_paused(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(
        db,
        sub.id,
        cfg,
        paused_at=datetime.now(UTC),
        anti_cheat={"camera": {"capture_snapshots": True}},
    )

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/snapshot?event_type=tab_switch",
        files={"frame": ("f.jpg", b"\xff\xd8\xff-not-a-real-jpeg", "image/jpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"stored": False, "paused": True}
    assert (await db.execute(select(QuizAttemptSnapshot))).first() is None


async def test_answering_while_paused_records_nothing(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, stepped=True, paused_at=datetime.now(UTC))

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/answer",
        data={"index": "0", "answer_0": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}"

    await db.refresh(attempt)
    assert attempt.current_index == 0
    assert (await db.execute(select(QuizAnswer))).first() is None


async def test_submitting_while_paused_does_not_wipe_answers(
    student_client: AsyncClient, db, student_user
) -> None:
    """submit_quiz deletes and regrades every answer — it must not run mid-pause."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, paused_at=datetime.now(UTC))
    db.add(
        QuizAnswer(
            attempt_id=attempt.id,
            question_id=0,
            answer={"selected": 1},
            is_correct=True,
            points_earned=1,
        )
    )
    await db.commit()

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/submit",
        data={"answer_0": "0", "answer_1": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(attempt)
    answers = (await db.execute(select(QuizAnswer))).scalars().all()
    assert [(a.question_id, a.points_earned) for a in answers] == [(0, 1)]
    assert attempt.status == QuizAttemptStatus.IN_PROGRESS
    assert attempt.score is None


# ── Resuming ─────────────────────────────────────────────────────────────────


async def test_resume_restores_the_question_and_the_clock(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    paused_for = 1800
    attempt = await _attempt(
        db,
        sub.id,
        cfg,
        time_limit_minutes=30,
        started_ago=60 + paused_for,
        paused_at=datetime.now(UTC) - timedelta(seconds=paused_for),
    )

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/quiz/{attempt.id}"

    await db.refresh(attempt)
    assert attempt.paused_at is None
    assert attempt.paused_seconds == pytest.approx(paused_for, abs=5)

    pause = (await db.execute(select(QuizAttemptPause))).first()
    assert pause is None  # no pause row was arranged; the column state is what matters

    # The question is back, and the clock picked up where it stopped. The live page seeds a
    # JS countdown with raw seconds (only the paused screen formats mm:ss), so assert on the
    # number it hands the client: 30 minutes less the 60s spent before the raid.
    page = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert QUESTION_TEXT in page.text
    seeded = re.search(r"var remaining = (\d+);", page.text)
    assert seeded is not None
    assert int(seeded.group(1)) == pytest.approx(1740, abs=5)


async def test_resume_closes_the_pause_record(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False)

    pause = (await db.execute(select(QuizAttemptPause))).scalar_one()
    assert pause.ended_at is not None
    assert pause.ended_at >= pause.started_at


async def test_resume_re_arms_anti_cheat(student_client: AsyncClient, db, student_user) -> None:
    """Violations count again once the student is back on the question page."""
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(
        db,
        sub.id,
        cfg,
        paused_at=datetime.now(UTC),
        anti_cheat={"rules": [{"event": "tab_switch", "threshold": 3, "action": {"type": "warn"}}]},
    )

    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/event", json={"type": "tab_switch"}
    )
    assert resp.json().get("paused") is None
    assert resp.json()["violation_count"] == 1

    await db.refresh(attempt)
    assert attempt.violations["tab_switch"] == 1


async def test_resuming_an_unpaused_attempt_is_harmless(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg)

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False
    )
    assert resp.status_code == 303
    await db.refresh(attempt)
    assert attempt.paused_at is None
    assert attempt.paused_seconds == 0


async def test_a_second_pause_accumulates_onto_the_first(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(db, sub.id, cfg, paused_seconds=300)

    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/pause", json=KHARKIV)
    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False)

    await db.refresh(attempt)
    assert attempt.paused_seconds >= 300
    assert len((await db.execute(select(QuizAttemptPause))).scalars().all()) == 1


async def test_another_student_cannot_resume_someone_elses_pause(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    sub, cfg = await _arrange(db, other.id)
    attempt = await _attempt(db, sub.id, cfg, paused_at=datetime.now(UTC))

    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False
    )
    assert resp.status_code == 403
    await db.refresh(attempt)
    assert attempt.paused_at is not None


# ── Stepper mode ─────────────────────────────────────────────────────────────


async def test_a_paused_stepper_attempt_keeps_its_question_clock(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    sub, cfg = await _arrange(db, student_user.student_id)
    attempt = await _attempt(
        db, sub.id, cfg, stepped=True, paused_at=datetime.now(UTC) - timedelta(seconds=900)
    )

    # Away far longer than the 60s question window, but nothing was burned.
    page = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert page.status_code == 200
    assert QUESTION_TEXT not in page.text

    await db.refresh(attempt)
    assert attempt.current_index == 0
    assert (await db.execute(select(QuizAnswer))).first() is None

    await student_client.post(f"/portal/quiz/{attempt.id}/airraid/resume", follow_redirects=False)
    await db.refresh(attempt)
    assert attempt.current_index == 0

    resumed = await student_client.get(f"/portal/quiz/{attempt.id}")
    assert QUESTION_TEXT in resumed.text
