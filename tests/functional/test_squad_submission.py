"""One upload serves the whole squad; pending invites block uploads; solo is untouched."""

from __future__ import annotations

import io
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    QuizAttempt,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    User,
)
from submissions_checker.db.models.enums import QuizAttemptStatus, UserRole
from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig
from submissions_checker.main import app
from submissions_checker.services import squads
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.md", "# r\n")
    return buf.getvalue()


def _client(user: User) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    authenticate(c, user)
    return c


async def _arrange(db, make_user, make_student, teacher, *, review_mode: str = "quiz_then_teacher"):
    """Subject with squads on, one quiz-first assignment, two enrolled consented students."""
    from datetime import UTC, datetime

    subject = Subject(name="Sq", owner_id=teacher.id, squad_max_size=2)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    quiz = {
        "questions": [
            {
                "type": "single_choice",
                "text": f"q{i}",
                "points": 1,
                "options": ["w", "r"],
                "correct": 1,
            }
            for i in range(4)
        ],
        "shuffle_questions": False,
        "shuffle_options": False,
        "pass_threshold_pct": 0.5,
        "max_quiz_attempts": 2,
    }
    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"h{subject.id}",
        config={
            "assignments": {
                "l1": {
                    "review_mode": review_mode,
                    "quiz": quiz,
                    "grading": {"code_weight": 0, "quiz_weight": 1},
                }
            }
        },
    )
    asg = SubjectsAssignment(
        subject_id=subject.id,
        title="L1",
        code="l1",
        min_grade=0,
        max_grade=8,
        config={"review_mode": review_mode, "grading": {"code_weight": 0, "quiz_weight": 1}},
    )
    db.add_all([cfg, asg])
    await db.commit()
    await db.refresh(asg)
    users, sas = [], []
    for name, uname in (("Anna A", "anna"), ("Bohdan B", "bohdan")):
        s = await make_student(full_name=name)
        s.recording_consent_at = datetime.now(UTC)
        u = await make_user(role=UserRole.STUDENT, username=uname, student=s)
        db.add(SubjectsStudents(subject_id=subject.id, student_id=s.id))
        sa = StudentAssignment(student_id=s.id, subjects_assignment_id=asg.id)
        db.add(sa)
        await db.commit()
        await db.refresh(sa)
        users.append(u)
        sas.append(sa)
    return subject, asg, users, sas


async def _lock_pair(db, subject, teacher, users) -> None:
    await squads.teacher_assign(
        db, subject.id, teacher.id, [users[0].student_id, users[1].student_id]
    )
    await db.commit()


async def test_one_upload_unlocks_the_quiz_for_both(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        r = await ca.post(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/submit",
            files={"file": ("r.zip", _zip(), "application/zip")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sub = (await db.execute(select(Submission))).scalar_one()
        assert sub.squad_id is not None

        page_b = await cb.get(f"/portal/subjects/{subject.id}/assignments/{sa_b.id}")
        assert "Пройти свою частину тесту" in page_b.text
        status_b = await cb.get(f"/portal/subjects/{subject.id}/assignments/{sa_b.id}/status")
        assert status_b.json()["status"] == "QUIZ_SENT"
        list_b = await cb.get(f"/portal/subjects/{subject.id}")
        assert "👥" in list_b.text

        # B's own (failed) attempt on the shared submission must count toward B's attempts
        # used, even though the submission's students_assignment_id belongs to A (the
        # uploader) — the shared submission's students_assignment_id is A's, not B's.
        from datetime import UTC, datetime

        db.add(
            QuizAttempt(
                submission_id=sub.id,
                student_id=ub.student_id,
                questions_snapshot=[],
                config_snapshot={"max_quiz_attempts": 2},
                started_at=datetime.now(UTC),
                status=QuizAttemptStatus.COMPLETED,
                is_passed=False,
                score=0,
                max_score=1,
            )
        )
        await db.commit()

        page_b_after = await cb.get(f"/portal/subjects/{subject.id}/assignments/{sa_b.id}")
        assert "1/2" in page_b_after.text
        page_a = await ca.get(f"/portal/subjects/{subject.id}/assignments/{sa_a.id}")
        assert "0/2" in page_a.text


async def test_second_member_cannot_upload_a_second_active_submission(
    db, make_user, make_student, teacher
) -> None:
    subject, asg, (ua, ub), (sa_a, sa_b) = await _arrange(db, make_user, make_student, teacher)
    await _lock_pair(db, subject, teacher, (ua, ub))
    async with _client(ua) as ca, _client(ub) as cb:
        await ca.post(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/submit",
            files={"file": ("r.zip", _zip(), "application/zip")},
        )
        sub = (await db.execute(select(Submission))).scalar_one()
        sub.status = "COMPLETED"  # type: ignore[assignment]
        await db.commit()
        r = await cb.post(
            f"/portal/subjects/{subject.id}/assignments/{sa_b.id}/submit",
            files={"file": ("r.zip", _zip(), "application/zip")},
        )
        assert r.status_code == 403  # "already passed" applies to the whole squad


async def test_pending_invite_blocks_upload(db, make_user, make_student, teacher) -> None:
    subject, asg, (ua, ub), (sa_a, _) = await _arrange(db, make_user, make_student, teacher)
    await squads.create_with_invites(db, subject.id, ua.student_id, [ub.student_id])
    await db.commit()
    async with _client(ua) as ca:
        r = await ca.post(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/submit",
            files={"file": ("r.zip", _zip(), "application/zip")},
        )
        assert r.status_code == 409


async def test_solo_student_on_squad_subject_is_unchanged(
    db, make_user, make_student, teacher
) -> None:
    subject, asg, (ua, _), (sa_a, _) = await _arrange(db, make_user, make_student, teacher)
    async with _client(ua) as ca:
        r = await ca.post(
            f"/portal/subjects/{subject.id}/assignments/{sa_a.id}/submit",
            files={"file": ("r.zip", _zip(), "application/zip")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        sub = (await db.execute(select(Submission))).scalar_one()
        assert sub.squad_id is None
        page = await ca.get(f"/portal/subjects/{subject.id}/assignments/{sa_a.id}")
        assert "Start Quiz" in page.text and "Пройти свою частину" not in page.text
