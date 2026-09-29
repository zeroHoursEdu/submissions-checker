"""Quiz lifecycle leaves a findable trail in the logs."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from structlog.testing import capture_logs

from tests.functional.test_student_quiz import _arrange_quiz, _consent, _make_attempt
from tests.functional.test_student_quiz_proctoring import _storage_enabled

pytestmark = pytest.mark.asyncio


def _one(logs: list[dict], event: str) -> dict:
    matches = [e for e in logs if e["event"] == event]
    assert matches, f"{event} not logged; got {[e['event'] for e in logs]}"
    return matches[-1]


async def test_start_then_resume(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    subject, sa, _sub, _cfg = await _arrange_quiz(db, student_user.student_id)
    url = f"/portal/subjects/{subject.id}/assignments/{sa.id}/quiz"
    with capture_logs() as logs:
        first = await student_client.get(url, follow_redirects=False)
        await student_client.get(url, follow_redirects=False)
    attempt_id = int(first.headers["location"].rsplit("/", 1)[-1])
    started = _one(logs, "quiz_attempt_started")
    assert started["attempt_id"] == attempt_id
    assert started["student_id"] == student_user.student_id
    assert started["question_count"] > 0
    resumed = _one(logs, "quiz_attempt_resumed")
    assert resumed["attempt_id"] == attempt_id
    assert "assignment_id" in resumed
    assert "squad_id" in resumed
    assert resumed["question_count"] > 0


async def test_submit_logs_the_finish(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)
    with capture_logs() as logs:
        await student_client.post(
            f"/portal/quiz/{attempt.id}/submit", data={}, follow_redirects=False
        )
    finished = _one(logs, "quiz_attempt_finished")
    assert finished["attempt_id"] == attempt.id
    assert finished["status"] == "COMPLETED"
    assert finished["log_level"] == "info"
    assert {"score", "max_score", "passed", "duration_s"} <= finished.keys()


async def test_violation_fail_finish_is_a_warning(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(
        db, sub.id, cfg, violations={"tab_switch": 3, "_force_fail": True}
    )
    with capture_logs() as logs:
        await student_client.post(
            f"/portal/quiz/{attempt.id}/submit", data={}, follow_redirects=False
        )
    finished = _one(logs, "quiz_attempt_finished")
    assert finished["status"] == "VIOLATION_FAIL" and finished["log_level"] == "warning"


async def test_snapshot_upload_failure_is_logged(
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
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    async with _storage_enabled() as storage:
        storage.upload_bytes.side_effect = RuntimeError("minio down")
        with capture_logs() as logs, pytest.raises(RuntimeError):
            await student_client.post(
                f"/portal/quiz/{attempt.id}/snapshot?event_type=camera_face_absent",
                files={"frame": ("f.jpg", jpeg, "image/jpeg")},
            )
    failed = _one(logs, "quiz_snapshot_failed")
    assert failed["attempt_id"] == attempt.id and failed["log_level"] == "error"


async def test_dispute_is_logged(student_client: AsyncClient, db, student_user) -> None:
    await _consent(db, student_user.student_id)
    _s, _sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    attempt = await _make_attempt(db, sub.id, cfg)
    with capture_logs() as logs:
        resp = await student_client.post(
            f"/portal/quiz/{attempt.id}/dispute", json={"question_id": 0, "note": "n"}
        )
    created = _one(logs, "quiz_dispute_created")
    assert created["dispute_id"] == resp.json()["dispute_id"]
    assert created["attempt_id"] == attempt.id
