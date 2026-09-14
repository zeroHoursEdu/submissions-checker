"""Unit tests for camera-proctoring building blocks (model, consent field, storage)."""

from __future__ import annotations

import pytest

from submissions_checker.core.config import Settings
from submissions_checker.db.models import QuizAttemptSnapshot, Student
from submissions_checker.services.storage import StorageService


def _settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="test-secret-key-minimum-32-chars-long",
        s3_endpoint_url="http://localstack:4566",
        s3_bucket_name="bucket",
    )


def test_snapshot_model_fields() -> None:
    snap = QuizAttemptSnapshot(
        attempt_id=1,
        event_type="camera_phone_detected",
        s3_key="proctoring/attempt-1/1-camera_phone_detected.jpg",
        s3_url="http://localstack:4566/bucket/proctoring/attempt-1/1.jpg",
        captured_at=None,
    )
    assert snap.attempt_id == 1
    assert snap.event_type == "camera_phone_detected"
    assert QuizAttemptSnapshot.__tablename__ == "quiz_attempt_snapshots"


def test_student_consent_field_defaults_none() -> None:
    student = Student(group_id=1, email="s@e.com", full_name="S")
    # Not yet consented until explicitly stamped.
    assert student.recording_consent_at is None


class _FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put_object(self, **kwargs) -> None:  # noqa: ANN003
        self.calls.append(kwargs)

    async def __aenter__(self) -> _FakeS3:
        return self

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None


class _FakeSession:
    def __init__(self, s3: _FakeS3) -> None:
        self._s3 = s3

    def client(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._s3


@pytest.mark.asyncio
async def test_upload_bytes_puts_object_with_content_type() -> None:
    storage = StorageService(_settings())
    fake = _FakeS3()
    storage._session = _FakeSession(fake)  # type: ignore[assignment]

    url = await storage.upload_bytes(
        b"\xff\xd8jpegbytes", "proctoring/attempt-9/1-x.jpg", "image/jpeg"
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Bucket"] == "bucket"
    assert call["Key"] == "proctoring/attempt-9/1-x.jpg"
    assert call["ContentType"] == "image/jpeg"
    assert call["Body"] == b"\xff\xd8jpegbytes"
    assert url.endswith("/bucket/proctoring/attempt-9/1-x.jpg")
