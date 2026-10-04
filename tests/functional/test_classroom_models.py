"""Round-trip of the Classroom ingest / LLM grading tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from submissions_checker.db.models.classroom import (
    ClassroomStudentLink,
    ClassroomWork,
    LLMGrading,
)
from submissions_checker.db.models.enums import ClassroomLinkMethod, LLMGradingStatus
from submissions_checker.db.models.google_connection import GoogleConnection
from submissions_checker.db.models.subject import Subject
from submissions_checker.db.models.subjects_assignment import SubjectsAssignment

pytestmark = pytest.mark.asyncio


async def test_classroom_rows_roundtrip(db, teacher, make_student):
    subject = Subject(name="S", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    asg = SubjectsAssignment(
        subject_id=subject.id, title="L", code="l", min_grade=0, max_grade=10, config={}
    )
    conn = GoogleConnection(
        user_id=teacher.id, google_email="t@edu.kpi.ua", refresh_token_enc="enc"
    )
    db.add_all([asg, conn])
    await db.commit()
    subject.classroom_course_id = "c1"
    subject.classroom_connection_id = conn.id
    asg.classroom_coursework_id = "w1"
    st = await make_student(full_name="Іван Комін")
    link = ClassroomStudentLink(
        subject_id=subject.id,
        classroom_user_id="u1",
        classroom_name="ІП-44 Komin Ivan",
        classroom_email="komin@edu.kpi.ua",
        student_id=st.id,
        method=ClassroomLinkMethod.EMAIL,
        confirmed=True,
    )
    db.add(link)
    await db.commit()
    work = ClassroomWork(
        subjects_assignment_id=asg.id,
        link_id=link.id,
        classroom_submission_id="s1",
        state="TURNED_IN",
        content_hash="a" * 64,
        manifest=[],
        seen_at=datetime.now(UTC),
    )
    db.add(work)
    await db.commit()
    g = LLMGrading(classroom_work_id=work.id, status=LLMGradingStatus.PENDING)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    assert g.attempts == 0 and g.status == "PENDING"
    assert conn.status == "ACTIVE"


async def test_link_unique_per_subject(db, teacher):
    subject = Subject(name="S", owner_id=teacher.id)
    db.add(subject)
    await db.commit()
    db.add_all(
        [
            ClassroomStudentLink(
                subject_id=subject.id, classroom_user_id="u", classroom_name="a", method="NONE"
            ),
            ClassroomStudentLink(
                subject_id=subject.id, classroom_user_id="u", classroom_name="b", method="NONE"
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await db.commit()
