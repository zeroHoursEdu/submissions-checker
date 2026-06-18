"""Hardened ZIP extraction guarding against Zip Slip and decompression bombs."""

from __future__ import annotations

import zipfile
from pathlib import Path

MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB
MAX_ENTRY_COUNT = 10_000


class UnsafeArchiveError(Exception):
    """Raised when an archive fails safety validation."""


def safe_extract(
    zip_file: zipfile.ZipFile,
    dest_dir: str | Path,
    *,
    max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_entries: int = MAX_ENTRY_COUNT,
) -> None:
    """Extract a ZipFile into dest_dir, rejecting path traversal and bombs.

    Raises UnsafeArchiveError on any unsafe entry before writing it.
    """
    dest = Path(dest_dir).resolve()
    infos = zip_file.infolist()
    if len(infos) > max_entries:
        raise UnsafeArchiveError(f"archive has too many entries ({len(infos)} > {max_entries})")
    total = sum(i.file_size for i in infos)
    if total > max_total_bytes:
        raise UnsafeArchiveError(f"archive too large uncompressed ({total} > {max_total_bytes} bytes)")
    for info in infos:
        name = info.filename
        # reject absolute paths and drive letters
        if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
            raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
        # reject symlinks (unix mode high bits 0xA000 == symlink)
        mode = info.external_attr >> 16
        if mode & 0o170000 == 0o120000:
            raise UnsafeArchiveError(f"symlink in archive: {name!r}")
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise UnsafeArchiveError(f"path traversal in archive: {name!r}")
    zip_file.extractall(dest)
