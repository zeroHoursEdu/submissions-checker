"""The one place submission ZIPs are written and read (MinIO first, legacy disk second)."""

from __future__ import annotations

import pytest

from submissions_checker.services.submission_files import (
    read_submission_zip,
    store_submission_zip,
    submission_key,
)
from tests.storage_fake import FakeStorage


def test_key_prefix() -> None:
    assert submission_key("12_ab.zip") == "submissions/12_ab.zip"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b.zip", "..\\x.zip", "../x.zip"])
def test_key_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(ValueError):
        submission_key(bad)


async def test_store_goes_to_storage_not_disk(tmp_path) -> None:
    storage = FakeStorage()
    await store_submission_zip(storage, "1_a.zip", b"PK", legacy_dir=tmp_path)
    assert storage.objects == {"submissions/1_a.zip": b"PK"}
    assert list(tmp_path.iterdir()) == []


async def test_store_without_storage_writes_legacy_dir(tmp_path) -> None:
    await store_submission_zip(None, "1_a.zip", b"PK", legacy_dir=tmp_path / "up")
    assert (tmp_path / "up" / "1_a.zip").read_bytes() == b"PK"


async def test_read_prefers_storage(tmp_path) -> None:
    storage = FakeStorage()
    storage.objects["submissions/1_a.zip"] = b"s3"
    (tmp_path / "1_a.zip").write_bytes(b"disk")
    assert await read_submission_zip(storage, "1_a.zip", legacy_dir=tmp_path) == b"s3"


async def test_read_falls_back_to_legacy_disk(tmp_path) -> None:
    (tmp_path / "1_a.zip").write_bytes(b"disk")
    assert await read_submission_zip(FakeStorage(), "1_a.zip", legacy_dir=tmp_path) == b"disk"
    assert await read_submission_zip(None, "1_a.zip", legacy_dir=tmp_path) == b"disk"


async def test_read_missing_everywhere_is_none(tmp_path) -> None:
    assert await read_submission_zip(FakeStorage(), "1_a.zip", legacy_dir=tmp_path) is None


@pytest.mark.parametrize("bad", [None, "", "../secret.zip", "a/b.zip"])
async def test_read_unsafe_or_empty_name_is_none(tmp_path, bad) -> None:
    (tmp_path.parent / "secret.zip").write_bytes(b"x")
    assert await read_submission_zip(FakeStorage(), bad, legacy_dir=tmp_path) is None
