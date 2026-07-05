"""Functional coverage for the STUDENT portal (``/portal*``).

Focus: permissions, ownership/enrollment, the recording-consent gate, ZIP
submission validation + DB side effects, and per-student notification
preferences. Behavior is asserted against what the handlers in
``api/routes/student_portal.py`` actually do — not what they ideally would.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from submissions_checker.db.models import (
    Student,
    StudentAssignment,
    Subject,
    SubjectsAssignment,
    SubjectsStudents,
    Submission,
    SubmissionSourceType,
    SubmissionStatus,
)
from submissions_checker.db.models.notification_preference import NotificationPreference
from submissions_checker.db.models.outbox import OutboxMessage
from submissions_checker.core.config import Settings, get_settings
from submissions_checker.main import app

pytestmark = pytest.mark.asyncio


# ── Arrangement helpers ──────────────────────────────────────────────────────


def _zip_bytes(files: dict[str, str] | None = None) -> bytes:
    files = files or {"main.py": "print('hi')\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def _make_subject(db, *, owner_id: int | None = None, name: str = "Algo") -> Subject:
    subject = Subject(name=name, owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    return subject


async def _enroll(db, student_id: int, subject_id: int) -> None:
    db.add(SubjectsStudents(student_id=student_id, subject_id=subject_id))
    await db.commit()


async def _make_assignment(
    db,
    subject_id: int,
    *,
    title: str = "HW1",
    deadline: datetime | None = None,
    config: dict | None = None,
    code: str | None = None,
) -> SubjectsAssignment:
    sa = SubjectsAssignment(
        subject_id=subject_id,
        title=title,
        deadline=deadline,
        config=config or {},
        code=code,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _make_student_assignment(
    db, student_id: int, subjects_assignment_id: int, *, grade: int | None = None
) -> StudentAssignment:
    sa = StudentAssignment(
        student_id=student_id,
        subjects_assignment_id=subjects_assignment_id,
        grade=grade,
    )
    db.add(sa)
    await db.commit()
    await db.refresh(sa)
    return sa


async def _consent(db, student_id: int) -> None:
    student = await db.get(Student, student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()


# ── Auth gates ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/portal",
        "/portal/consent",
        "/portal/summary",
        "/portal/subjects/1",
        "/portal/subjects/1/assignments/1",
        "/portal/notification-preferences",
    ],
)
async def test_anonymous_rejected(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize("path", ["/portal", "/portal/summary", "/portal/notification-preferences"])
async def test_teacher_forbidden(teacher_client: AsyncClient, path: str) -> None:
    # StudentUser is STUDENT-only; TEACHER hits the role guard.
    assert (await teacher_client.get(path)).status_code == 403


@pytest.mark.parametrize("path", ["/portal", "/portal/summary"])
async def test_admin_forbidden(admin_client: AsyncClient, path: str) -> None:
    assert (await admin_client.get(path)).status_code == 403


# NOTE: The StudentId dependency's "no linked student record → 404" branch is
# unreachable in practice: a DB CHECK constraint (ck_users_student_role_has_student_id)
# forbids inserting a STUDENT-role user with a NULL student_id, so this path cannot be
# exercised at the API layer.


# ── Consent gate ─────────────────────────────────────────────────────────────


async def test_subjects_grid_redirects_to_consent_when_unconsented(
    student_client: AsyncClient,
) -> None:
    resp = await student_client.get("/portal", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/consent"


async def test_consent_page_shown_when_unconsented(student_client: AsyncClient) -> None:
    resp = await student_client.get("/portal/consent", follow_redirects=False)
    assert resp.status_code == 200


async def test_consent_notice_defaults_to_ukrainian_vocab_text(
    student_client: AsyncClient,
) -> None:
    """No operator override configured -> the notice comes from i18n/uk.yml's
    consent.notice_text, not a hardcoded English default."""
    resp = await student_client.get("/portal/consent", follow_redirects=False)
    assert resp.status_code == 200
    assert "веб-камеру" in resp.text
    assert "Some quizzes in this course are proctored" not in resp.text


async def test_consent_notice_operator_override_takes_precedence(
    student_client: AsyncClient,
) -> None:
    """A jurisdiction-specific override configured via settings still wins over
    the localized default."""
    base = dict(
        secret_key="test-secret-key-minimum-32-chars-long",
        recording_consent_notice="Custom jurisdiction-specific legal text.",
    )
    settings = Settings(**base)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        resp = await student_client.get("/portal/consent", follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert resp.status_code == 200
    assert "Custom jurisdiction-specific legal text." in resp.text


async def test_accept_consent_sets_timestamp_and_redirects(
    student_client: AsyncClient, db, student_user
) -> None:
    resp = await student_client.post("/portal/consent", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"
    student = await db.get(Student, student_user.student_id)
    await db.refresh(student)
    assert student.recording_consent_at is not None


async def test_consent_page_redirects_to_grid_once_consented(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    resp = await student_client.get("/portal/consent", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


async def test_grid_renders_only_enrolled_subjects(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    enrolled = await _make_subject(db, name="Enrolled")
    await _make_subject(db, name="NotEnrolled")
    await _enroll(db, student_user.student_id, enrolled.id)
    resp = await student_client.get("/portal")
    assert resp.status_code == 200
    assert "Enrolled" in resp.text
    assert "NotEnrolled" not in resp.text


# ── Enrollment / ownership ───────────────────────────────────────────────────


async def test_assignments_list_not_enrolled_returns_404(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)  # not enrolled
    resp = await student_client.get(f"/portal/subjects/{subject.id}")
    assert resp.status_code == 404


async def test_assignments_list_enrolled_ok(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    resp = await student_client.get(f"/portal/subjects/{subject.id}")
    assert resp.status_code == 200


async def test_assignment_detail_of_other_student_returns_404(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    """assignment_detail filters by student_id, so another student's SA is 404."""
    await _consent(db, student_user.student_id)
    other = await make_student()
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    other_sa = await _make_student_assignment(db, other.id, sub_a.id)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{other_sa.id}"
    )
    assert resp.status_code == 404


async def test_assignment_detail_own_ok(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)
    resp = await student_client.get(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}"
    )
    assert resp.status_code == 200


# ── Submission upload ────────────────────────────────────────────────────────


async def test_submit_creates_pending_submission_and_outbox(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("solution.zip", _zip_bytes(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/portal/subjects/{subject.id}/assignments/{sa.id}"
    )

    rows = (
        await db.execute(
            select(Submission).where(Submission.students_assignment_id == sa.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    sub = rows[0]
    assert sub.status == SubmissionStatus.PENDING
    assert sub.source_type == SubmissionSourceType.ZIP_UPLOAD
    assert sub.source_metadata["original_filename"] == "solution.zip"
    assert "saved_as" in sub.source_metadata

    outbox = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.payload["submission_id"].astext == str(sub.id)
            )
        )
    ).scalars().all()
    assert len(outbox) == 1


async def test_submit_non_zip_rejected_400(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("solution.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
    assert "ZIP" in resp.json()["detail"]


async def test_submit_oversized_rejected_413(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)

    big = b"\0" * (50 * 1024 * 1024 + 1)
    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("big.zip", big, "application/zip")},
    )
    assert resp.status_code == 413


async def test_submit_to_other_students_assignment_404(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    await _consent(db, student_user.student_id)
    other = await make_student()
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    other_sa = await _make_student_assignment(db, other.id, sub_a.id)

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{other_sa.id}/submit",
        files={"file": ("x.zip", _zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 404
    # No submission row created for the victim's assignment.
    rows = (
        await db.execute(
            select(Submission).where(Submission.students_assignment_id == other_sa.id)
        )
    ).scalars().all()
    assert rows == []


async def test_submit_blocked_after_deadline_default_policy(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    # deadline column is TIMESTAMP WITHOUT TIME ZONE; handler treats naive as UTC.
    past = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    sub_a = await _make_assignment(db, subject.id, deadline=past)  # default late_policy=block
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("x.zip", _zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 403
    assert "deadline" in resp.json()["detail"].lower()


async def test_submit_allowed_after_deadline_when_policy_allow(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    past = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None)
    sub_a = await _make_assignment(
        db, subject.id, deadline=past, config={"late_policy": "allow"}
    )
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("x.zip", _zip_bytes(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303


async def test_submit_blocked_when_already_completed(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)
    db.add(
        Submission(
            students_assignment_id=sa.id,
            source_type=SubmissionSourceType.ZIP_UPLOAD,
            source_metadata={},
            status=SubmissionStatus.COMPLETED,
        )
    )
    await db.commit()

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("x.zip", _zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 403
    assert "already passed" in resp.json()["detail"].lower()


async def test_submit_max_submissions_enforced(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id, config={"max_submissions": 1})
    sa = await _make_student_assignment(db, student_user.student_id, sub_a.id)
    db.add(
        Submission(
            students_assignment_id=sa.id,
            source_type=SubmissionSourceType.ZIP_UPLOAD,
            source_metadata={},
            status=SubmissionStatus.PENDING,
        )
    )
    await db.commit()

    resp = await student_client.post(
        f"/portal/subjects/{subject.id}/assignments/{sa.id}/submit",
        files={"file": ("x.zip", _zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 403
    assert "Maximum number of submissions" in resp.json()["detail"]


# ── Summary ──────────────────────────────────────────────────────────────────


async def test_summary_renders_for_consented_student(
    student_client: AsyncClient, db, student_user
) -> None:
    await _consent(db, student_user.student_id)
    subject = await _make_subject(db)
    await _enroll(db, student_user.student_id, subject.id)
    sub_a = await _make_assignment(db, subject.id)
    await _make_student_assignment(db, student_user.student_id, sub_a.id, grade=80)
    resp = await student_client.get("/portal/summary")
    assert resp.status_code == 200


# ── Notification preferences ─────────────────────────────────────────────────


async def test_notification_prefs_page_ok(student_client: AsyncClient) -> None:
    resp = await student_client.get("/portal/notification-preferences")
    assert resp.status_code == 200


async def test_toggle_creates_disabled_pref_then_flips(
    student_client: AsyncClient, db, student_user
) -> None:
    # First toggle: no row exists → creates a row with enabled=False.
    resp = await student_client.post(
        "/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    pref = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.student_id == student_user.student_id,
                NotificationPreference.case == "SUBMISSION_CHECKED",
                NotificationPreference.method == "EMAIL",
            )
        )
    ).scalar_one()
    assert pref.enabled is False

    # Second toggle flips it back to True.
    await student_client.post(
        "/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle",
        follow_redirects=False,
    )
    await db.refresh(pref)
    assert pref.enabled is True


async def test_notification_pref_is_per_student(
    student_client: AsyncClient, db, student_user, make_student
) -> None:
    """Toggling one student's pref must not create/alter another student's pref."""
    other = await make_student()
    await student_client.post(
        "/portal/notification-preferences/SUBMISSION_CHECKED/EMAIL/toggle",
        follow_redirects=False,
    )
    other_rows = (
        await db.execute(
            select(NotificationPreference).where(
                NotificationPreference.student_id == other.id
            )
        )
    ).scalars().all()
    assert other_rows == []
