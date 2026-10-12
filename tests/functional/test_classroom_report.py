"""Teacher-only report of why the overnight grader gave a work its points."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from submissions_checker.db.models import StudentAssignment, Subject
from submissions_checker.db.models.enums import LLMGradingStatus, UserRole
from tests.functional.conftest import authenticate
from tests.functional.test_classroom_board import _board, _link, _work, _world

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _keep_loaded(db):
    db.sync_session.expire_on_commit = False


def _report(subject, grading) -> str:
    return f"/teacher/subjects/{subject.id}/classroom/gradings/{grading.id}/report"


async def test_report_explains_each_criterion(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(
        db,
        asg,
        await _link(db, subject, st),
        model="claude-opus-5-5",
        graded_at=datetime(2026, 10, 12, 1, 5, tzinfo=UTC),
    )

    page = await teacher_client.get(_report(subject, grading))

    assert page.status_code == 200
    text = page.text
    assert "Комін Тарас" in text and "Lab" in text
    assert "Звіт" in text and "4/5" in text
    assert "Звіт майже повний" in text and "розділ 2" in text
    assert "Загалом добре" in text
    assert "claude-opus-5-5" in text
    assert "звіт.pdf" in text
    # The criterion the model does not grade is named as the teacher's.
    assert "Зірочка" in text and "оцінює викладач" in text


async def test_report_shows_teacher_changes_after_approval(
    teacher_client: AsyncClient, db, teacher, make_student
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(
        db,
        asg,
        await _link(db, subject, st),
        approved_by=teacher.id,
        approved_at=datetime.now(UTC),
    )
    db.add(
        StudentAssignment(
            student_id=st.id, subjects_assignment_id=asg.id, teacher_scores={"report": 2}
        )
    )
    await db.commit()

    text = (await teacher_client.get(_report(subject, grading))).text

    assert "Затверджено" in text
    assert "змінено викладачем" in text and "2/5" in text


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (LLMGradingStatus.PENDING, None, "Ще не оцінено"),
        (LLMGradingStatus.FAILED, "judge timed out", "judge timed out"),
    ],
)
async def test_report_without_a_draft(
    teacher_client: AsyncClient, db, teacher, make_student, status, error, expected
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(db, asg, await _link(db, subject, st), status=status, error=error)
    page = await teacher_client.get(_report(subject, grading))
    assert page.status_code == 200 and expected in page.text


async def test_report_is_scoped_to_the_subject(
    teacher_client: AsyncClient, db, teacher, make_student, make_user
):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(db, asg, await _link(db, subject, st))
    other = Subject(name="Other", owner_id=teacher.id)
    db.add(other)
    await db.commit()

    assert (await teacher_client.get(_report(other, grading))).status_code == 404

    stranger = await make_user(role=UserRole.TEACHER, username="stranger")
    authenticate(teacher_client, stranger)
    assert (await teacher_client.get(_report(subject, grading))).status_code in (403, 404)


async def test_board_links_to_the_report(teacher_client: AsyncClient, db, teacher, make_student):
    subject, asg, st = await _world(db, teacher, make_student)
    _, grading = await _work(db, asg, await _link(db, subject, st))
    page = await teacher_client.get(_board(subject, asg))
    assert _report(subject, grading) in page.text
