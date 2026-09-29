"""Teachers can read an attempt's anti-cheat timeline; nobody else can."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from submissions_checker.db.models import QuizAttemptEvent
from submissions_checker.db.models.enums import QuizAttemptStatus, QuizEventOutcome
from tests.functional.test_student_quiz_proctoring import _arrange_quiz, _make_attempt

pytestmark = pytest.mark.asyncio


async def _attempt_with_events(db, student_user, owner_id: int | None):
    subject, sa, sub, cfg = await _arrange_quiz(db, student_user.student_id)
    subject.owner_id = owner_id
    attempt = await _make_attempt(
        db,
        sub.id,
        cfg,
        status=QuizAttemptStatus.VIOLATION_FAIL,
        violations={"tab_switch": 3, "_force_fail": True},
    )
    db.add_all(
        [
            QuizAttemptEvent(
                attempt_id=attempt.id,
                event_type="tab_switch",
                count_after=3,
                rule_threshold=3,
                action="fail",
                outcome=QuizEventOutcome.APPLIED,
                client_ctx={"vw": 1920, "vh": 1080},
            ),
            QuizAttemptEvent(
                attempt_id=attempt.id,
                event_type="tab_return",
                action="none",
                outcome=QuizEventOutcome.INFORMATIONAL,
                client_ctx={"away_ms": 42000},
            ),
            QuizAttemptEvent(
                attempt_id=attempt.id,
                event_type="window_blur",
                action="none",
                outcome=QuizEventOutcome.IGNORED_PAUSED,
                client_ctx={},
            ),
        ]
    )
    await db.commit()
    return subject, sa, attempt


async def test_owner_sees_the_timeline(
    teacher_client: AsyncClient, teacher, db, student_user
) -> None:
    _s, _sa, attempt = await _attempt_with_events(db, student_user, teacher.id)
    resp = await teacher_client.get(f"/teacher/quiz-attempts/{attempt.id}/events")
    assert resp.status_code == 200
    html = resp.text
    assert "tab_switch" in html and "tab_return" in html and "window_blur" in html
    assert "42" in html  # away_ms rendered as seconds
    assert "1920×1080" in html
    assert "VIOLATION_FAIL" in html


async def test_admin_sees_any_timeline(admin_client: AsyncClient, db, student_user) -> None:
    _s, _sa, attempt = await _attempt_with_events(db, student_user, None)
    resp = await admin_client.get(f"/teacher/quiz-attempts/{attempt.id}/events")
    assert resp.status_code == 200


async def test_other_teacher_is_refused(
    teacher_client: AsyncClient, make_user, db, student_user
) -> None:
    other = await make_user(role="TEACHER")
    _s, _sa, attempt = await _attempt_with_events(db, student_user, other.id)
    resp = await teacher_client.get(f"/teacher/quiz-attempts/{attempt.id}/events")
    assert resp.status_code == 403


async def test_unknown_attempt_is_404(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get("/teacher/quiz-attempts/999999/events")
    assert resp.status_code == 404


async def test_students_cannot_open_it(student_client: AsyncClient, db, student_user) -> None:
    _s, _sa, attempt = await _attempt_with_events(db, student_user, None)
    resp = await student_client.get(f"/teacher/quiz-attempts/{attempt.id}/events")
    assert resp.status_code == 403


async def test_assignment_table_links_the_auto_fail_badge_to_the_timeline(
    teacher_client: AsyncClient, teacher, db, student_user
) -> None:
    subject, sa, attempt = await _attempt_with_events(db, student_user, teacher.id)
    resp = await teacher_client.get(
        f"/teacher/subjects/{subject.id}/assignments/{sa.subjects_assignment_id}"
    )
    assert resp.status_code == 200
    assert f"/teacher/quiz-attempts/{attempt.id}/events" in resp.text
