"""Proctoring evidence is readable only through an authorized application endpoint.

Webcam frames were previously written public-read and handed to the browser as direct
object-storage URLs. Deploying that publicly would make every student's webcam image
retrievable by anyone holding the URL. These tests pin the replacement: object storage is
never addressed by the client, and the application checks subject authorization before a
single byte is returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

import submissions_checker.api.routes.teacher_portal as teacher_portal_module
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
    User,
)
from submissions_checker.db.models.enums import QuizAttemptStatus, UserRole
from submissions_checker.main import app

pytestmark = pytest.mark.asyncio

IMAGE_BYTES = b"\xff\xd8\xffnot-a-real-jpeg"


async def _arrange_snapshot(db, *, owner_id: int, student_id: int) -> QuizAttemptSnapshot:
    """A proctoring snapshot belonging to a subject owned by ``owner_id``."""
    subject = Subject(name=f"Subject-{owner_id}", owner_id=owner_id)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)

    db.add(SubjectsStudents(student_id=student_id, subject_id=subject.id))
    await db.commit()

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
        status=SubmissionStatus.COMPLETED,
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    attempt = QuizAttempt(
        submission_id=submission.id,
        status=QuizAttemptStatus.COMPLETED,
        started_at=datetime.now(UTC),
        questions_snapshot=[],
        config_snapshot={},
        violations={},
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)

    snapshot = QuizAttemptSnapshot(
        attempt_id=attempt.id,
        event_type="facelost",
        s3_key=f"proctoring/attempt-{attempt.id}/1-facelost.jpg",
        s3_url="http://minio:9000/bucket/whatever.jpg",
        captured_at=datetime.now(UTC),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


@pytest.fixture(autouse=True)
def storage_configured():
    """The endpoint only serves evidence when object storage is configured."""
    app.dependency_overrides[get_settings] = lambda: Settings(
        secret_key="test-secret-key-minimum-32-chars-long",
        s3_bucket_name="proctor-bucket",
        s3_endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        aws_region="us-east-1",
    )
    yield
    app.dependency_overrides.pop(get_settings, None)


def _stub_storage(content: bytes = IMAGE_BYTES):
    storage = AsyncMock()
    storage.download_bytes = AsyncMock(return_value=content)
    return patch.object(teacher_portal_module, "StorageService", return_value=storage), storage


async def test_owning_teacher_receives_the_image(
    teacher_client: AsyncClient, db, teacher: User, student_user: User
) -> None:
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)

    patcher, storage = _stub_storage()
    with patcher:
        response = await teacher_client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code == 200
    assert response.content == IMAGE_BYTES
    assert response.headers["content-type"].startswith("image/")
    storage.download_bytes.assert_awaited_once_with(snapshot.s3_key)


async def test_teacher_of_another_subject_is_refused(
    teacher_client: AsyncClient, db, teacher: User, student_user: User, make_user
) -> None:
    other_teacher = await make_user(role=UserRole.TEACHER, username="other-teacher")
    snapshot = await _arrange_snapshot(
        db, owner_id=other_teacher.id, student_id=student_user.student_id
    )

    patcher, storage = _stub_storage()
    with patcher:
        response = await teacher_client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code == 403
    assert IMAGE_BYTES not in response.content
    storage.download_bytes.assert_not_awaited()


async def test_student_is_refused(
    student_client: AsyncClient, db, teacher: User, student_user: User
) -> None:
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)

    patcher, storage = _stub_storage()
    with patcher:
        response = await student_client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code in (401, 403)
    storage.download_bytes.assert_not_awaited()


async def test_anonymous_request_is_refused(
    client: AsyncClient, db, teacher: User, student_user: User
) -> None:
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)

    patcher, storage = _stub_storage()
    with patcher:
        response = await client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code in (401, 403)
    storage.download_bytes.assert_not_awaited()


async def test_unknown_snapshot_is_not_found(teacher_client: AsyncClient) -> None:
    patcher, _ = _stub_storage()
    with patcher:
        response = await teacher_client.get("/teacher/proctoring/snapshots/999999")

    assert response.status_code == 404


async def test_admin_may_view_any_snapshot(
    admin_client: AsyncClient, db, teacher: User, student_user: User
) -> None:
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)

    patcher, storage = _stub_storage()
    with patcher:
        response = await admin_client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code == 200
    storage.download_bytes.assert_awaited_once()


async def test_uploads_are_not_written_with_a_public_acl() -> None:
    """A public ACL would make the object readable to anyone holding the key."""
    import inspect

    from submissions_checker.services.storage import StorageService

    for method in (StorageService.upload_bytes, StorageService.upload_file):
        source = inspect.getsource(method)
        assert "public-read" not in source, f"{method.__name__} still sets a public ACL"


async def test_snapshot_upload_response_carries_no_object_storage_url(
    student_client: AsyncClient, db, student_user: User
) -> None:
    """The client must never be handed a durable, publicly fetchable evidence URL."""
    import submissions_checker.api.routes.student_quiz as student_quiz_module
    from submissions_checker.db.models.subject_plugin_config import SubjectPluginConfig

    student = await db.get(Student, student_user.student_id)
    student.recording_consent_at = datetime.now(UTC)
    await db.commit()

    subject = Subject(name="Proctored", owner_id=None)
    db.add(subject)
    await db.commit()
    await db.refresh(subject)
    db.add(SubjectsStudents(student_id=student_user.student_id, subject_id=subject.id))
    await db.commit()

    cfg = SubjectPluginConfig(
        subject_id=subject.id,
        version=1,
        content_hash=f"hash-upload-{subject.id}",
        config={"assignments": {"hw1": {"quiz": {}}}},
    )
    db.add(cfg)
    sub_a = SubjectsAssignment(subject_id=subject.id, title="HW1", code="hw1", config={})
    db.add(sub_a)
    await db.commit()
    await db.refresh(cfg)
    await db.refresh(sub_a)

    sa = StudentAssignment(student_id=student_user.student_id, subjects_assignment_id=sub_a.id)
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

    attempt = QuizAttempt(
        submission_id=submission.id,
        status=QuizAttemptStatus.IN_PROGRESS,
        started_at=datetime.now(UTC),
        questions_snapshot=[],
        config_snapshot={"anti_cheat": {"camera": {"capture_snapshots": True}}},
        violations={},
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)

    fake_storage = AsyncMock()
    fake_storage.upload_bytes = AsyncMock(return_value="http://minio:9000/bucket/x.jpg")
    with patch.object(student_quiz_module, "StorageService", return_value=fake_storage):
        response = await student_client.post(
            f"/portal/quiz/{attempt.id}/snapshot?event_type=facelost",
            files={"frame": ("f.jpg", b"\xff\xd8\xffdata", "image/jpeg")},
        )

    assert response.status_code == 200
    assert response.json() == {"stored": True}
    assert "minio" not in response.text and "http" not in response.text


async def test_unreadable_object_is_reported_not_crashed(
    teacher_client: AsyncClient, db, teacher: User, student_user: User
) -> None:
    """A missing or unreachable object must 404, not raise out of the handler."""
    snapshot = await _arrange_snapshot(db, owner_id=teacher.id, student_id=student_user.student_id)

    storage = AsyncMock()
    storage.download_bytes = AsyncMock(side_effect=RuntimeError("NoSuchKey"))
    with patch.object(teacher_portal_module, "StorageService", return_value=storage):
        response = await teacher_client.get(f"/teacher/proctoring/snapshots/{snapshot.id}")

    assert response.status_code == 404
    assert "no longer available" in response.text.lower()
