"""Submission ZIP storage: MinIO first, the legacy local ``uploads/`` directory second.

The local fallback exists only while a deployment still holds ZIPs written before they
moved to object storage (``python -m submissions_checker.cli.migrate_uploads`` copies
them across). Every reader and writer goes through here so removing the fallback later
is a change to this file alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from submissions_checker.services.storage import StorageService

LEGACY_UPLOADS_DIR = Path("uploads")
_PREFIX = "submissions/"


def submission_key(saved_as: str) -> str:
    """Object key for a stored submission; rejects anything but a plain file name."""
    if not saved_as or saved_as in {".", ".."} or "/" in saved_as or "\\" in saved_as:
        raise ValueError(f"unsafe submission file name: {saved_as!r}")
    return f"{_PREFIX}{saved_as}"


async def store_submission_zip(
    storage: StorageService | None,
    saved_as: str,
    data: bytes,
    legacy_dir: Path = LEGACY_UPLOADS_DIR,
) -> None:
    key = submission_key(saved_as)
    if storage is not None:
        await storage.upload_bytes(data, key, content_type="application/zip")
        return
    # No object store configured (tests, bare local runs): keep the old behaviour.
    legacy_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread((legacy_dir / saved_as).write_bytes, data)


async def read_submission_zip(
    storage: StorageService | None,
    saved_as: str | None,
    legacy_dir: Path = LEGACY_UPLOADS_DIR,
) -> bytes | None:
    """The ZIP's bytes, or None when it is stored nowhere (or the name is unsafe)."""
    if not saved_as:
        return None
    try:
        key = submission_key(saved_as)
    except ValueError:
        return None
    if storage is not None:
        data = await storage.try_download_bytes(key)
        if data is not None:
            return data
    path = legacy_dir / saved_as
    if await asyncio.to_thread(path.is_file):
        return await asyncio.to_thread(path.read_bytes)
    return None
