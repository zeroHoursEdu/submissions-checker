"""Unit tests for StorageService.

aioboto3 is patched at the SDK boundary: the session's async client context
manager yields a mock S3 client. No network / S3 / localstack is touched. Tests
assert put/delete args, key handling, and the three URL-construction branches.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from submissions_checker.core.config import Settings
from submissions_checker.services.storage import StorageService


def _settings(**overrides) -> Settings:
    base = dict(
        secret_key="test-secret-key-minimum-32-chars-long",
        s3_bucket_name="my-bucket",
        s3_endpoint_url=None,
        s3_public_base_url=None,
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        aws_region="us-east-1",
    )
    base.update(overrides)
    return Settings(**base)


def _service_with_mock_client(settings: Settings):
    """Build a StorageService whose aioboto3 session yields a mock S3 client.

    Returns (service, s3_client_mock, client_call_recorder).
    """
    s3_client = AsyncMock()

    calls: dict[str, object] = {}

    @asynccontextmanager
    async def fake_client(service_name, *, endpoint_url=None):
        calls["service_name"] = service_name
        calls["endpoint_url"] = endpoint_url
        yield s3_client

    with patch("submissions_checker.services.storage.aioboto3.Session") as session_cls:
        session = MagicMock()
        session.client = fake_client
        session_cls.return_value = session
        svc = StorageService(settings)
    return svc, s3_client, calls, session_cls


def test_init_passes_credentials_to_session() -> None:
    with patch("submissions_checker.services.storage.aioboto3.Session") as session_cls:
        StorageService(_settings(aws_region="eu-west-1"))
    session_cls.assert_called_once_with(
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="eu-west-1",
    )


async def test_upload_bytes_puts_object_without_public_acl() -> None:
    """Objects must not be world-readable: a leaked key would be a leaked webcam frame.

    Proctoring evidence is served only through the authenticated teacher endpoint, and
    the bucket is never exposed, so a public-read ACL here would reintroduce exactly the
    exposure that design removes.
    """
    svc, s3, calls, _ = _service_with_mock_client(_settings())
    url = await svc.upload_bytes(b"hello", "path/to/file.txt", content_type="text/plain")

    s3.put_object.assert_awaited_once_with(
        Bucket="my-bucket",
        Key="path/to/file.txt",
        Body=b"hello",
        ContentType="text/plain",
    )
    _, kwargs = s3.put_object.call_args
    assert "ACL" not in kwargs
    assert calls["service_name"] == "s3"
    # default amazonaws URL branch (no endpoint, no public base)
    assert url == "https://my-bucket.s3.amazonaws.com/path/to/file.txt"


async def test_upload_bytes_default_content_type() -> None:
    svc, s3, _, _ = _service_with_mock_client(_settings())
    await svc.upload_bytes(b"x", "k")
    _, kwargs = s3.put_object.call_args
    assert kwargs["ContentType"] == "application/octet-stream"


async def test_upload_file_streams_handle_and_builds_url(tmp_path: Path) -> None:
    f = tmp_path / "art.bin"
    f.write_bytes(b"payload")
    svc, s3, _, _ = _service_with_mock_client(_settings())

    url = await svc.upload_file(f, "subjects/art.bin")

    s3.put_object.assert_awaited_once()
    _, kwargs = s3.put_object.call_args
    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["Key"] == "subjects/art.bin"
    assert "ACL" not in kwargs, "uploads must not be world-readable"
    # Body is the opened file handle pointing at the local file (closed by the
    # time control returns here, since upload_file streams it inside a `with`).
    assert Path(kwargs["Body"].name) == f
    assert url == "https://my-bucket.s3.amazonaws.com/subjects/art.bin"


async def test_delete_file_calls_delete_object() -> None:
    svc, s3, _, _ = _service_with_mock_client(_settings())
    await svc.delete_file("old/key")
    s3.delete_object.assert_awaited_once_with(Bucket="my-bucket", Key="old/key")


async def test_endpoint_url_passed_to_client_and_url() -> None:
    svc, s3, calls, _ = _service_with_mock_client(
        _settings(s3_endpoint_url="http://localstack:4566/")
    )
    url = await svc.upload_bytes(b"x", "k.txt")
    assert calls["endpoint_url"] == "http://localstack:4566/"
    # endpoint branch: <endpoint>/<bucket>/<key>, trailing slash stripped
    assert url == "http://localstack:4566/my-bucket/k.txt"


async def test_public_base_url_wins_over_endpoint() -> None:
    svc, _, _, _ = _service_with_mock_client(
        _settings(
            s3_endpoint_url="http://localstack:4566",
            s3_public_base_url="https://cdn.example.com/assets/",
        )
    )
    url = await svc.upload_bytes(b"x", "k.txt")
    assert url == "https://cdn.example.com/assets/k.txt"


def test_build_url_branches_directly() -> None:
    with patch("submissions_checker.services.storage.aioboto3.Session"):
        default = StorageService(_settings())
        assert default._build_url("a/b") == "https://my-bucket.s3.amazonaws.com/a/b"

        endpoint = StorageService(_settings(s3_endpoint_url="http://e:9000"))
        assert endpoint._build_url("a/b") == "http://e:9000/my-bucket/a/b"

        public = StorageService(_settings(s3_public_base_url="https://p/"))
        assert public._build_url("a/b") == "https://p/a/b"


async def test_download_bytes_reads_the_object_back() -> None:
    """The authenticated routes read private objects back through this path."""
    svc, s3, _, _ = _service_with_mock_client(_settings())
    body = AsyncMock()
    body.read = AsyncMock(return_value=b"frame-bytes")
    s3.get_object = AsyncMock(return_value={"Body": body})

    data = await svc.download_bytes("proctoring/attempt-1/1-facelost.jpg")

    assert data == b"frame-bytes"
    s3.get_object.assert_awaited_once_with(
        Bucket="my-bucket", Key="proctoring/attempt-1/1-facelost.jpg"
    )
