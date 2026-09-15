"""GET /metrics through the real app."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_metrics_is_unauthenticated_text_exposition(client: AsyncClient) -> None:
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in resp.text
    assert "students_total" in resp.text


async def test_metrics_endpoint_does_not_count_itself(client: AsyncClient) -> None:
    from submissions_checker.core import metrics

    own = metrics.http_requests_total.labels(route="/metrics", method="GET", status_class="2xx")
    before = own._value.get()
    await client.get("/metrics")
    await client.get("/metrics")
    assert own._value.get() == before
    assert metrics.http_requests_in_progress._value.get() == 0


async def test_login_page_is_counted_by_template(client: AsyncClient) -> None:
    await client.get("/auth/login")
    body = (await client.get("/metrics")).text
    assert 'http_requests_total{method="GET",route="/auth/login",status_class="2xx"}' in body


# ── Business counters ────────────────────────────────────────────────────────

from submissions_checker.core import metrics  # noqa: E402
from submissions_checker.db.models.enums import QuizAttemptStatus, UserRole  # noqa: E402
from tests.functional import test_quiz_air_raid as air_raid_mod  # noqa: E402
from tests.functional import test_quiz_disputes as disputes_mod  # noqa: E402
from tests.functional import test_quiz_first_and_stepper as stepper_mod  # noqa: E402
from tests.functional import test_student_portal as portal_mod  # noqa: E402
from tests.functional.test_student_quiz import _arrange_quiz, _consent  # noqa: E402

PASSWORD = "correct-horse-battery-staple"
air_raid_over = air_raid_mod.air_raid_over  # re-export the fixture into this module


def _counter(counter, **labels) -> float:
    return (counter.labels(**labels) if labels else counter)._value.get()


async def test_login_increments_logins_by_role(client: AsyncClient, make_user) -> None:
    await make_user(role=UserRole.TEACHER, username="alice", password=PASSWORD)
    before = _counter(metrics.logins_total, role="TEACHER")
    await client.post(
        "/auth/login", data={"username": "alice", "password": PASSWORD}, follow_redirects=False
    )
    await client.post(
        "/auth/login", data={"username": "alice", "password": "wrong"}, follow_redirects=False
    )
    assert _counter(metrics.logins_total, role="TEACHER") == before + 1


async def test_quiz_start_and_finish_are_counted(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, _submission, _cfg = await _arrange_quiz(db, student_user.student_id)
    started = _counter(metrics.quiz_attempts_started_total)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz", follow_redirects=False
    )
    assert resp.status_code == 303
    assert _counter(metrics.quiz_attempts_started_total) == started + 1

    attempt_id = int(resp.headers["location"].rstrip("/").split("/")[-1])
    finished = _counter(metrics.quiz_attempts_finished_total, status="COMPLETED")
    passed = _counter(metrics.quiz_attempts_passed_total)
    # QUIZ_CONFIG has two single-choice questions whose correct index is 1.
    await student_client.post(
        f"/portal/quiz/{attempt_id}/submit",
        data={"answer_0": "1", "answer_1": "1"},
        follow_redirects=False,
    )
    assert _counter(metrics.quiz_attempts_finished_total, status="COMPLETED") == finished + 1
    assert _counter(metrics.quiz_attempts_passed_total) == passed + 1


async def test_stepped_answer_is_counted(
    student_client: AsyncClient, db, student_user, teacher
) -> None:
    subject, sa, submission = await stepper_mod._arrange(
        db, student_user.student_id, teacher.id, quiz_cfg=stepper_mod.STEPPED_QUIZ
    )
    await stepper_mod._start(student_client, subject, sa)
    attempt = await stepper_mod._attempt_of(db, submission.id)
    before = _counter(metrics.quiz_answers_total)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/answer",
        data={"index": "0", "answer_0": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _counter(metrics.quiz_answers_total) == before + 1


async def test_submission_upload_is_counted(student_client: AsyncClient, db, student_user) -> None:
    await portal_mod._consent(db, student_user.student_id)
    subject = await portal_mod._make_subject(db)
    await portal_mod._enroll(db, student_user.student_id, subject.id)
    sub_a = await portal_mod._make_assignment(db, subject.id)
    sa = await portal_mod._make_student_assignment(db, student_user.student_id, sub_a.id)
    before = _counter(metrics.submissions_uploaded_total)
    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("solution.zip", portal_mod._zip_bytes(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _counter(metrics.submissions_uploaded_total) == before + 1


async def test_dispute_open_and_resolve_are_counted(
    client: AsyncClient, student_client: AsyncClient, db, student_user, make_user, login
) -> None:
    owner = await make_user(role=UserRole.TEACHER, username="owner")
    _s, _sa, sub, cfg = await disputes_mod._arrange_quiz(
        db, student_user.student_id, owner_id=owner.id
    )
    attempt = await disputes_mod._make_attempt(db, sub.id, cfg, status=QuizAttemptStatus.COMPLETED)
    opened = _counter(metrics.disputes_opened_total)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": "key is wrong"}
    )
    assert resp.status_code == 200
    assert _counter(metrics.disputes_opened_total) == opened + 1

    rejected = _counter(metrics.disputes_resolved_total, status="REJECTED")
    login(client, owner)
    resp = await client.post(
        f"/teacher/disputes/{resp.json()['dispute_id']}/resolve",
        data={"action": "reject", "note": "the key is right"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _counter(metrics.disputes_resolved_total, status="REJECTED") == rejected + 1


async def test_air_raid_pause_is_counted(
    student_client: AsyncClient, db, student_user, air_raid_over
) -> None:
    await air_raid_mod._consent(db, student_user.student_id)
    sub, cfg = await air_raid_mod._arrange(db, student_user.student_id)
    attempt = await air_raid_mod._attempt(db, sub.id, cfg)
    before = _counter(metrics.air_raid_pauses_total)
    resp = await student_client.post(
        f"/portal/quiz/{attempt.id}/airraid/pause", json=air_raid_mod.KHARKIV
    )
    assert resp.json()["paused"] is True
    assert _counter(metrics.air_raid_pauses_total) == before + 1


# ── Gauge refresh queries ────────────────────────────────────────────────────


async def test_compute_gauges_counts_real_students_only(db, make_student) -> None:
    from submissions_checker.db.models.enums import EntityType
    from submissions_checker.workers.scheduled.metrics_refresh import compute_gauges

    await make_student(email="r1@x.y")
    await make_student(email="r2@x.y")
    test_student = await make_student(email="t@x.y")
    test_student.type = EntityType.TEST
    await db.commit()

    values = await compute_gauges(db)
    assert values["students_total"] == 2
    assert values["students_active_7d"] == 0
    assert values["outbox_pending"] == 0
    assert values["outbox_oldest_pending_age_seconds"] == 0


async def test_compute_gauges_sees_logins_and_pending_outbox(
    client: AsyncClient, db, make_user
) -> None:
    from submissions_checker.db.models import OutboxMessage
    from submissions_checker.db.models.enums import OutboxEventType
    from submissions_checker.workers.scheduled.metrics_refresh import compute_gauges

    await make_user(role=UserRole.STUDENT, username="bob", password=PASSWORD)
    await client.post(
        "/auth/login", data={"username": "bob", "password": PASSWORD}, follow_redirects=False
    )
    db.add(OutboxMessage(event_type=OutboxEventType.NEW_SUBMISSION, payload={"submission_id": 1}))
    await db.commit()

    values = await compute_gauges(db)
    assert values["students_active_1d"] == 1
    assert values["students_active_30d"] == 1
    assert values["outbox_pending"] == 1
    assert values["outbox_oldest_pending_age_seconds"] >= 0
