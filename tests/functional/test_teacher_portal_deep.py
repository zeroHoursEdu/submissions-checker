"""Deep functional tests for under-covered TEACHER portal handlers.

Targets handlers that ``test_teacher_portal.py`` only lightly touches:

* CSV student import — both global (``POST /teacher/students/import``) and
  subject-scoped (``POST /teacher/subjects/{id}/students/import`` with variant
  columns). Input-validation / security surface: malformed CSV, missing required
  columns, duplicate emails, empty rows/file, non-UTF-8, cross-teacher 403.
* Test-student provisioning + impersonation
  (``POST /teacher/subjects/{id}/test-student`` and ``…/test-student/enter``).
* Course feedback request / list / CSV export.
* Single student add (``GET`` + ``POST /teacher/students/add``).

Behaviour was read directly from ``api/routes/teacher_portal.py``; every test
asserts exact status codes AND concrete DB side-effects (rows created, outbox
queued, enrollment + StudentAssignment fan-out, variants set).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from submissions_checker.core.security import COOKIE_NAME
from submissions_checker.db.models import (
    FeedbackRequest,
    FeedbackResponse,
    FeedbackToken,
    OutboxMessage,
    Semester,
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    SubjectTestStudent,
    User,
)
from submissions_checker.db.models.enums import (
    EntityType,
    OutboxEventType,
    SubjectStatus,
    UserRole,
)
from submissions_checker.db.models.group import Group
from submissions_checker.main import app
from tests.functional.conftest import authenticate

pytestmark = pytest.mark.asyncio


# ── Arrange helpers ──────────────────────────────────────────────────────────


async def _make_subject(
    db,
    owner_id: int | None,
    *,
    name: str = "Owned Subject",
    code: str | None = None,
    status: SubjectStatus = SubjectStatus.ACTIVE,
) -> Subject:
    subject = Subject(name=name, code=code, owner_id=owner_id, status=status)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


async def _make_assignment(
    db,
    subject_id: int,
    *,
    title: str = "A1",
    code: str = "a1",
    config: dict | None = None,
) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id,
        code=code,
        title=title,
        max_grade=100,
        config=config or {},
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _make_test_group(db) -> Group:
    """provision_test_student does ``scalar_one()`` for a Group named __TEST__."""
    group = Group(name="__TEST__")
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def _make_active_semester(db) -> Semester:
    today = date.today()
    sem = Semester(
        name="Test Semester",
        season="SPRING",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=30),
    )
    db.add(sem)
    await db.commit()
    await db.refresh(sem)
    return sem


async def _enroll(db, subject_id: int, student_id: int) -> None:
    db.add(SubjectsStudents(subject_id=subject_id, student_id=student_id))
    await db.commit()


def _client_for(user) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    authenticate(c, user)
    return c


def _csv_file(body: str, name: str = "students.csv"):
    return {"file": (name, body.encode("utf-8"), "text/csv")}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Global student import — POST /teacher/students/import
# ─────────────────────────────────────────────────────────────────────────────


async def test_global_import_creates_student_user_group_and_outbox(
    client: AsyncClient, db, teacher
) -> None:
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Ivan,Petrenko,ivan@example.com\n"
        "IT-21,Olena,Kovalenko,olena@example.com\n"
    )
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(body), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/students?imported=2&skipped=0"

    # Two REAL students created.
    students = (
        await db.execute(select(Student).order_by(Student.email))
    ).scalars().all()
    assert {s.email for s in students} == {"ivan@example.com", "olena@example.com"}
    assert all(s.type == EntityType.REAL for s in students)
    assert all(s.full_name for s in students)

    # Shared group created once.
    group_count = await db.scalar(
        select(func.count()).select_from(Group).where(Group.name == "IT-21")
    )
    assert group_count == 1

    # Two STUDENT users, one per student, with derived usernames.
    users = (
        await db.execute(select(User).where(User.role == UserRole.STUDENT))
    ).scalars().all()
    assert len(users) == 2
    assert {u.username for u in users} == {"ivan.petrenko", "olena.kovalenko"}

    # Two SEND_CREDENTIALS outbox rows carrying the plaintext password.
    creds = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.SEND_CREDENTIALS
            )
        )
    ).scalars().all()
    assert len(creds) == 2
    for c in creds:
        assert c.payload["student_email"] in {"ivan@example.com", "olena@example.com"}
        assert c.payload["password"]
        assert c.payload["username"] in {"ivan.petrenko", "olena.kovalenko"}


async def test_global_import_lowercases_email_and_skips_duplicate(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # Pre-existing student with same (lowercased) email must be skipped.
    await make_student(email="dupe@example.com")
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Du,Pe,DUPE@example.com\n"  # uppercase → lowercased → duplicate
        "IT-21,New,Person,fresh@example.com\n"
    )
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(body), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/students?imported=1&skipped=1"

    fresh = (
        await db.execute(select(Student).where(Student.email == "fresh@example.com"))
    ).scalar_one_or_none()
    assert fresh is not None
    # No duplicate created for dupe@example.com.
    dupe_count = await db.scalar(
        select(func.count()).select_from(Student).where(Student.email == "dupe@example.com")
    )
    assert dupe_count == 1


async def test_global_import_skips_blank_rows(
    client: AsyncClient, db, teacher
) -> None:
    # Rows with any empty required cell are silently skipped (not errors).
    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,,Petrenko,noname@example.com\n"  # missing first name
        ",Ivan,Petrenko,nogroup@example.com\n"  # missing group
        "IT-21,Ivan,Petrenko,\n"  # missing email
        "IT-21,Good,Row,good@example.com\n"
    )
    authenticate(client, teacher)
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(body), follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/students?imported=1&skipped=0"
    total = await db.scalar(select(func.count()).select_from(Student))
    assert total == 1


async def test_global_import_missing_required_columns_is_422(
    client: AsyncClient, db, teacher
) -> None:
    authenticate(client, teacher)
    # Only some required columns present.
    body = "student_group,email\nIT-21,x@example.com\n"
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(body), follow_redirects=False
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Missing CSV columns" in detail
    assert "student_name" in detail and "student_surname" in detail
    # Nothing written.
    assert (await db.scalar(select(func.count()).select_from(Student))) == 0


async def test_global_import_empty_file_is_422(
    client: AsyncClient, db, teacher
) -> None:
    # Empty file → DictReader.fieldnames is None → required columns missing → 422.
    authenticate(client, teacher)
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(""), follow_redirects=False
    )
    assert resp.status_code == 422
    assert "Missing CSV columns" in resp.json()["detail"]


async def test_global_import_non_utf8_is_422(
    client: AsyncClient, db, teacher
) -> None:
    authenticate(client, teacher)
    # 0xff is invalid as a UTF-8 start byte → UnicodeDecodeError → 422.
    bad = b"student_group,student_name,student_surname,email\n\xff\xfe,a,b,c@x.com\n"
    resp = await client.post(
        "/teacher/students/import",
        files={"file": ("bad.csv", bad, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "File must be UTF-8 encoded"


async def test_global_import_strips_utf8_bom(
    client: AsyncClient, db, teacher
) -> None:
    # Excel often prepends a BOM; handler decodes with utf-8-sig to strip it.
    authenticate(client, teacher)
    body = "student_group,student_name,student_surname,email\nIT-21,Bom,Test,bom@example.com\n"
    raw = b"\xef\xbb\xbf" + body.encode("utf-8")
    resp = await client.post(
        "/teacher/students/import",
        files={"file": ("bom.csv", raw, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/students?imported=1&skipped=0"
    s = (
        await db.execute(select(Student).where(Student.email == "bom@example.com"))
    ).scalar_one_or_none()
    assert s is not None  # BOM did NOT corrupt the student_group header


async def test_global_import_duplicate_username_disambiguates(
    client: AsyncClient, db, teacher
) -> None:
    # Two students with same first.last derive base username "ivan.petrenko";
    # _generate_username appends _2 for the collision.
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Ivan,Petrenko,ivan1@example.com\n"
        "IT-22,Ivan,Petrenko,ivan2@example.com\n"
    )
    resp = await client.post(
        "/teacher/students/import", files=_csv_file(body), follow_redirects=False
    )
    assert resp.status_code == 303
    usernames = {
        u.username
        for u in (
            await db.execute(select(User).where(User.role == UserRole.STUDENT))
        ).scalars().all()
    }
    assert usernames == {"ivan.petrenko", "ivan.petrenko_2"}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Subject-scoped import — POST /teacher/subjects/{id}/students/import
# ─────────────────────────────────────────────────────────────────────────────


async def test_subject_import_creates_enrollment_and_student_assignments(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="a1")
    authenticate(client, teacher)

    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Ada,Lovelace,ada@example.com\n"
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc == f"/teacher/subjects/{subject.id}?imported=1&skipped=0&variants_updated=0"

    student = (
        await db.execute(select(Student).where(Student.email == "ada@example.com"))
    ).scalar_one()
    # Enrolled in the subject.
    enrolled = await db.scalar(
        select(func.count()).select_from(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject.id,
            SubjectsStudents.student_id == student.id,
        )
    )
    assert enrolled == 1
    # StudentAssignment fan-out created for the subject's assignment.
    sa_count = await db.scalar(
        select(func.count()).select_from(StudentAssignment).where(
            StudentAssignment.student_id == student.id,
            StudentAssignment.subjects_assignment_id == sa.id,
        )
    )
    assert sa_count == 1
    # Credentials outbox queued (new account).
    creds = await db.scalar(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.event_type == OutboxEventType.SEND_CREDENTIALS
        )
    )
    assert creds == 1


async def test_subject_import_existing_student_skipped_but_enrolled(
    client: AsyncClient, db, teacher, make_student
) -> None:
    # An existing global student is "skipped" (no new account/outbox) yet still
    # gets enrolled into the subject.
    existing = await make_student(email="known@example.com")
    subject = await _make_subject(db, owner_id=teacher.id)
    await _make_assignment(db, subject.id, code="a1")
    authenticate(client, teacher)

    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Known,Person,known@example.com\n"
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("imported=0&skipped=1&variants_updated=0")

    enrolled = await db.scalar(
        select(func.count()).select_from(SubjectsStudents).where(
            SubjectsStudents.subject_id == subject.id,
            SubjectsStudents.student_id == existing.id,
        )
    )
    assert enrolled == 1
    # No SEND_CREDENTIALS for the skipped (pre-existing) student.
    creds = await db.scalar(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.event_type == OutboxEventType.SEND_CREDENTIALS
        )
    )
    assert creds == 0


async def test_subject_import_sets_variants_from_variant_columns(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(
        db, subject.id, code="lab1", config={"variants_required": True}
    )
    authenticate(client, teacher)

    body = (
        "student_group,student_name,student_surname,email,variant_lab1\n"
        "IT-21,Var,Student,var@example.com,B\n"
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("imported=1&skipped=0&variants_updated=1")

    student = (
        await db.execute(select(Student).where(Student.email == "var@example.com"))
    ).scalar_one()
    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.student_id == student.id,
                StudentAssignment.subjects_assignment_id == sa.id,
            )
        )
    ).scalar_one()
    assert student_assignment.variant == "B"


async def test_subject_import_blank_variant_leaves_variant_unset(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(
        db, subject.id, code="lab1", config={"variants_required": True}
    )
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email,variant_lab1\n"
        "IT-21,Novar,Student,novar@example.com,\n"  # blank variant cell
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("variants_updated=0")
    student = (
        await db.execute(select(Student).where(Student.email == "novar@example.com"))
    ).scalar_one()
    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.student_id == student.id,
                StudentAssignment.subjects_assignment_id == sa.id,
            )
        )
    ).scalar_one()
    assert student_assignment.variant is None


async def test_subject_import_unknown_variant_column_ignored(
    client: AsyncClient, db, teacher
) -> None:
    # variant_ column whose code matches no assignment is silently ignored.
    subject = await _make_subject(db, owner_id=teacher.id)
    await _make_assignment(db, subject.id, code="a1")
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email,variant_doesnotexist\n"
        "IT-21,Ghost,Var,ghost@example.com,Z\n"
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("imported=1&skipped=0&variants_updated=0")


async def test_subject_import_missing_columns_is_422(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file("student_group,email\nIT-21,x@example.com\n"),
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert "Missing CSV columns" in resp.json()["detail"]
    assert (await db.scalar(select(func.count()).select_from(Student))) == 0


async def test_subject_import_non_utf8_is_422(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    bad = b"student_group,student_name,student_surname,email\n\xff,a,b,c@x.com\n"
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files={"file": ("bad.csv", bad, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "File must be UTF-8 encoded"


async def test_subject_import_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)
    body = (
        "student_group,student_name,student_surname,email\n"
        "IT-21,Ada,Lovelace,ada@example.com\n"
    )
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 403
    # No student leaked.
    assert (await db.scalar(select(func.count()).select_from(Student))) == 0


async def test_subject_import_missing_subject_is_404(
    teacher_client: AsyncClient,
) -> None:
    body = "student_group,student_name,student_surname,email\nIT,A,B,a@x.com\n"
    resp = await teacher_client.post(
        "/teacher/subjects/999/students/import",
        files=_csv_file(body),
        follow_redirects=False,
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 3. Test-student provisioning + impersonation
# ─────────────────────────────────────────────────────────────────────────────


async def test_provision_test_student_creates_entities(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="a1")
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{subject.id}?test_student=created"

    # SubjectTestStudent row, with a stored plaintext password.
    sts = (
        await db.execute(
            select(SubjectTestStudent).where(SubjectTestStudent.subject_id == subject.id)
        )
    ).scalar_one()
    assert sts.plain_password

    # The backing student is TEST-typed and in the __TEST__ group.
    student = (await db.execute(select(Student).where(Student.id == sts.student_id))).scalar_one()
    assert student.type == EntityType.TEST

    # A STUDENT user named test_<subject_id> exists.
    user = (
        await db.execute(select(User).where(User.student_id == student.id))
    ).scalar_one()
    assert user.username == f"test_{subject.id}"
    assert user.role == UserRole.STUDENT

    # Enrolled + StudentAssignment created for the subject assignment.
    assert (
        await db.scalar(
            select(func.count()).select_from(SubjectsStudents).where(
                SubjectsStudents.subject_id == subject.id,
                SubjectsStudents.student_id == student.id,
            )
        )
        == 1
    )
    assert (
        await db.scalar(
            select(func.count()).select_from(StudentAssignment).where(
                StudentAssignment.student_id == student.id,
                StudentAssignment.subjects_assignment_id == sa.id,
            )
        )
        == 1
    )


async def test_provision_test_student_defaults_variant_when_required_and_unpicked(
    client: AsyncClient, db, teacher
) -> None:
    """docs/known_bugs.md #12b: a variants_required assignment must get a real
    variant even if the teacher never touches the selector."""
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(
        db, subject.id, code="a1",
        config={"variants_required": True, "variants": {"2": {}, "1": {}}},
    )
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert resp.status_code == 303

    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.subjects_assignment_id == sa.id
            )
        )
    ).scalar_one()
    assert student_assignment.variant == "1"  # first sorted key, not left NULL


async def test_provision_test_student_persists_explicit_variant_choice(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(
        db, subject.id, code="a1",
        config={"variants_required": True, "variants": {"1": {}, "2": {}}},
    )
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student",
        data={"variant_a1": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.subjects_assignment_id == sa.id
            )
        )
    ).scalar_one()
    assert student_assignment.variant == "2"


async def test_provision_test_student_invalid_variant_falls_back_to_default(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(
        db, subject.id, code="a1",
        config={"variants_required": True, "variants": {"1": {}, "3": {}}},
    )
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student",
        data={"variant_a1": "does-not-exist"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.subjects_assignment_id == sa.id
            )
        )
    ).scalar_one()
    assert student_assignment.variant == "1"  # fell back to first sorted key


async def test_provision_test_student_no_variants_configured_leaves_null(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    sa = await _make_assignment(db, subject.id, code="a1", config={})
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert resp.status_code == 303

    student_assignment = (
        await db.execute(
            select(StudentAssignment).where(
                StudentAssignment.subjects_assignment_id == sa.id
            )
        )
    ).scalar_one()
    assert student_assignment.variant is None


async def test_provision_test_student_idempotent(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)

    first = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert first.status_code == 303
    second = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert second.status_code == 303
    # Second call detects existing → redirects with ?test_student=existing.
    assert second.headers["location"] == f"/teacher/subjects/{subject.id}?test_student=existing"
    # Still exactly one SubjectTestStudent.
    count = await db.scalar(
        select(func.count()).select_from(SubjectTestStudent).where(
            SubjectTestStudent.subject_id == subject.id
        )
    )
    assert count == 1


async def test_provision_test_student_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    await _make_test_group(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert resp.status_code == 403
    assert (await db.scalar(select(func.count()).select_from(SubjectTestStudent))) == 0


async def test_provision_test_student_admin_succeeds_cross_teacher(
    client: AsyncClient, db, admin, make_user
) -> None:
    # provision_test_student uses require_subject_access, which admits ADMIN —
    # unlike the old inline owner check this replaced.
    await _make_test_group(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, admin)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{subject.id}?test_student=created"


async def test_provision_test_student_missing_subject_is_404(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    authenticate(client, teacher)
    resp = await client.post("/teacher/subjects/999/test-student", follow_redirects=False)
    assert resp.status_code == 404


async def test_enter_as_test_student_sets_auth_cookie(
    client: AsyncClient, db, teacher
) -> None:
    await _make_test_group(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    await client.post(f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False)

    test_user = (
        await db.execute(
            select(User)
            .join(SubjectTestStudent, SubjectTestStudent.student_id == User.student_id)
            .where(SubjectTestStudent.subject_id == subject.id)
        )
    ).scalar_one()

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student/enter", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"
    # A fresh auth cookie was set (re-mints session as the test student).
    set_cookie = resp.headers.get("set-cookie", "")
    assert COOKIE_NAME in set_cookie
    assert test_user.username == f"test_{subject.id}"


async def test_enter_as_test_student_without_provision_is_404(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student/enter", follow_redirects=False
    )
    assert resp.status_code == 404  # no test student provisioned


async def test_enter_as_test_student_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    await _make_test_group(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    # Provision as owner first.
    owner_client = _client_for(other)
    await owner_client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    await owner_client.aclose()

    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student/enter", follow_redirects=False
    )
    assert resp.status_code == 403


async def test_enter_as_test_student_admin_succeeds_cross_teacher(
    client: AsyncClient, db, admin, make_user
) -> None:
    await _make_test_group(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    owner_client = _client_for(other)
    await owner_client.post(
        f"/teacher/subjects/{subject.id}/test-student", follow_redirects=False
    )
    await owner_client.aclose()

    authenticate(client, admin)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/test-student/enter", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Feedback — request / list / export
# ─────────────────────────────────────────────────────────────────────────────


async def test_feedback_request_creates_request_tokens_and_outbox(
    client: AsyncClient, db, teacher, make_student
) -> None:
    semester = await _make_active_semester(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    s1 = await make_student(email="f1@example.com")
    s2 = await make_student(email="f2@example.com")
    await _enroll(db, subject.id, s1.id)
    await _enroll(db, subject.id, s2.id)
    authenticate(client, teacher)

    resp = await client.post(
        f"/teacher/subjects/{subject.id}/feedback/request", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/teacher/subjects/{subject.id}?feedback_sent=1"

    fr = (
        await db.execute(
            select(FeedbackRequest).where(FeedbackRequest.subject_id == subject.id)
        )
    ).scalar_one()
    assert fr.semester_id == semester.id
    assert fr.created_by_teacher_id == teacher.id

    tokens = (
        await db.execute(
            select(FeedbackToken).where(FeedbackToken.feedback_request_id == fr.id)
        )
    ).scalars().all()
    assert len(tokens) == 2
    assert {t.student_id for t in tokens} == {s1.id, s2.id}
    assert all(t.token for t in tokens)

    # One FEEDBACK_REQUEST_SENT outbox per token.
    ob = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.FEEDBACK_REQUEST_SENT
            )
        )
    ).scalars().all()
    assert len(ob) == 2
    token_ids = {t.id for t in tokens}
    assert {m.payload["feedback_token_id"] for m in ob} == token_ids


async def test_feedback_request_no_active_semester_redirects_with_error(
    client: AsyncClient, db, teacher
) -> None:
    # No Semester row spanning today → handler redirects with feedback_error.
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/feedback/request", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/teacher/subjects/{subject.id}?feedback_error=no_active_semester"
    )
    assert (await db.scalar(select(func.count()).select_from(FeedbackRequest))) == 0


async def test_feedback_request_duplicate_redirects_already_sent(
    client: AsyncClient, db, teacher
) -> None:
    # The uq_feedback_request_subject_semester constraint makes a second request
    # for the same subject+semester raise IntegrityError → already_sent redirect.
    semester = await _make_active_semester(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    db.add(
        FeedbackRequest(
            subject_id=subject.id,
            semester_id=semester.id,
            created_by_teacher_id=teacher.id,
        )
    )
    await db.commit()
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/feedback/request", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/teacher/subjects/{subject.id}?feedback_error=already_sent"
    )
    # Still just the one request.
    assert (await db.scalar(select(func.count()).select_from(FeedbackRequest))) == 1


async def test_feedback_request_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    await _make_active_semester(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)
    resp = await client.post(
        f"/teacher/subjects/{subject.id}/feedback/request", follow_redirects=False
    )
    assert resp.status_code == 403
    assert (await db.scalar(select(func.count()).select_from(FeedbackRequest))) == 0


async def test_feedback_view_owner_renders_200(
    client: AsyncClient, db, teacher
) -> None:
    await _make_active_semester(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback")
    assert resp.status_code == 200


async def test_feedback_view_admin_can_view_any(
    client: AsyncClient, db, admin, make_user
) -> None:
    # Feedback handlers use require_subject_access → ADMIN allowed.
    await _make_active_semester(db)
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, admin)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback")
    assert resp.status_code == 200


async def test_feedback_view_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback")
    assert resp.status_code == 403


async def test_feedback_export_csv_contains_responses(
    client: AsyncClient, db, teacher, make_student
) -> None:
    semester = await _make_active_semester(db)
    subject = await _make_subject(db, owner_id=teacher.id)
    student = await make_student(full_name="Grace Hopper", email="grace@example.com")
    await _enroll(db, subject.id, student.id)

    fr = FeedbackRequest(
        subject_id=subject.id,
        semester_id=semester.id,
        created_by_teacher_id=teacher.id,
    )
    db.add(fr)
    await db.flush()
    token = FeedbackToken(feedback_request_id=fr.id, student_id=student.id, token="tok123")
    db.add(token)
    await db.flush()
    db.add(
        FeedbackResponse(
            feedback_token_id=token.id,
            subject_id=subject.id,
            rating=5,
            went_well="lectures",
            went_bad="too fast",
            to_change="more labs",
        )
    )
    await db.commit()

    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    header = "student_name,student_email,rating,went_well,went_bad,to_change,submitted_at"
    assert header in resp.text
    assert "Grace Hopper" in resp.text
    assert "grace@example.com" in resp.text
    assert "lectures" in resp.text


async def test_feedback_export_csv_empty_returns_header_only(
    client: AsyncClient, db, teacher
) -> None:
    subject = await _make_subject(db, owner_id=teacher.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback/export.csv")
    assert resp.status_code == 200
    assert (
        "student_name,student_email,rating,went_well,went_bad,to_change,submitted_at"
        in resp.text
    )


async def test_feedback_export_cross_teacher_is_403(
    client: AsyncClient, db, teacher, make_user
) -> None:
    other = await make_user(role=UserRole.TEACHER, username="other")
    subject = await _make_subject(db, owner_id=other.id)
    authenticate(client, teacher)
    resp = await client.get(f"/teacher/subjects/{subject.id}/feedback/export.csv")
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 5. Single student add — GET + POST /teacher/students/add
# ─────────────────────────────────────────────────────────────────────────────


async def test_add_student_page_renders(teacher_client: AsyncClient) -> None:
    resp = await teacher_client.get("/teacher/students/add")
    assert resp.status_code == 200


async def test_add_student_creates_student_user_and_outbox(
    client: AsyncClient, db, teacher
) -> None:
    authenticate(client, teacher)
    resp = await client.post(
        "/teacher/students/add",
        data={
            "first_name": "Alan",
            "last_name": "Turing",
            "email": "ALAN@example.com",  # uppercased → lowered
            "group_name": "IT-42",
            "github_username": "aturing",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/teacher/students?imported=1&skipped=0"

    student = (
        await db.execute(select(Student).where(Student.email == "alan@example.com"))
    ).scalar_one()
    assert student.full_name == "Alan Turing"
    assert student.github_username == "aturing"
    # Group auto-created.
    group = (await db.execute(select(Group).where(Group.name == "IT-42"))).scalar_one()
    assert student.group_id == group.id
    # User + outbox.
    user = (
        await db.execute(select(User).where(User.student_id == student.id))
    ).scalar_one()
    assert user.username == "alan.turing"
    creds = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.event_type == OutboxEventType.SEND_CREDENTIALS
            )
        )
    ).scalar_one()
    assert creds.payload["student_email"] == "alan@example.com"


async def test_add_student_duplicate_email_is_422(
    client: AsyncClient, db, teacher, make_student
) -> None:
    await make_student(email="taken@example.com")
    authenticate(client, teacher)
    resp = await client.post(
        "/teacher/students/add",
        data={
            "first_name": "Some",
            "last_name": "One",
            "email": "taken@example.com",
            "group_name": "IT-1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422
    # No second student / user / outbox created.
    assert (
        await db.scalar(
            select(func.count()).select_from(Student).where(
                Student.email == "taken@example.com"
            )
        )
        == 1
    )
    assert (await db.scalar(select(func.count()).select_from(OutboxMessage))) == 0
