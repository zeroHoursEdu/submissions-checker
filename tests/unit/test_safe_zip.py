"""Unit tests for utils.safe_zip.safe_extract — Zip Slip / bomb / symlink guards.

In-memory zips are crafted with zipfile + BytesIO; extraction targets a pytest
tmp_path so no shared filesystem state is touched.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from submissions_checker.utils.safe_zip import (
    UnsafeArchiveError,
    safe_extract,
)

SYMLINK_MODE = (0o120000 | 0o777) << 16  # S_IFLNK in external_attr high bits


def _zip(entries: list[tuple[str, bytes]], *, symlinks: set[str] | None = None) -> zipfile.ZipFile:
    """Build an in-memory ZipFile. `symlinks` names get the symlink mode bit set."""
    symlinks = symlinks or set()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            if name in symlinks:
                info.external_attr = SYMLINK_MODE
            zf.writestr(info, data)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def test_accepts_normal_zip(tmp_path: Path) -> None:
    zf = _zip([("a.txt", b"hello"), ("sub/b.txt", b"world")])
    safe_extract(zf, tmp_path)
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert (tmp_path / "sub" / "b.txt").read_text() == "world"


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    zf = _zip([("../escape.txt", b"x")])
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        safe_extract(zf, tmp_path)


def test_rejects_deep_parent_traversal(tmp_path: Path) -> None:
    zf = _zip([("ok/../../escape.txt", b"x")])
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        safe_extract(zf, tmp_path)


def test_rejects_absolute_unix_path(tmp_path: Path) -> None:
    zf = _zip([("/etc/passwd", b"x")])
    with pytest.raises(UnsafeArchiveError, match="absolute"):
        safe_extract(zf, tmp_path)


def test_rejects_windows_drive_path(tmp_path: Path) -> None:
    zf = _zip([("C:windows", b"x")])
    with pytest.raises(UnsafeArchiveError, match="absolute"):
        safe_extract(zf, tmp_path)


def test_rejects_backslash_absolute_path(tmp_path: Path) -> None:
    zf = _zip([("\\\\server\\share", b"x")])
    with pytest.raises(UnsafeArchiveError, match="absolute"):
        safe_extract(zf, tmp_path)


def test_rejects_symlink_entry(tmp_path: Path) -> None:
    zf = _zip([("link", b"/etc/passwd")], symlinks={"link"})
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        safe_extract(zf, tmp_path)


def test_rejects_too_many_entries(tmp_path: Path) -> None:
    zf = _zip([(f"f{i}.txt", b"") for i in range(5)])
    with pytest.raises(UnsafeArchiveError, match="too many entries"):
        safe_extract(zf, tmp_path, max_entries=4)


def test_rejects_oversized_uncompressed(tmp_path: Path) -> None:
    # A "zip bomb": file_size reported in the central directory exceeds the cap.
    zf = _zip([("big.bin", b"A" * 1000)])
    with pytest.raises(UnsafeArchiveError, match="too large"):
        safe_extract(zf, tmp_path, max_total_bytes=500)


def test_accepts_at_exact_limits(tmp_path: Path) -> None:
    # entries == max and total == max are allowed (strict > comparisons).
    payload = b"A" * 10
    zf = _zip([("a.txt", payload), ("b.txt", payload)])
    safe_extract(zf, tmp_path, max_entries=2, max_total_bytes=20)
    assert (tmp_path / "a.txt").read_bytes() == payload


def test_traversal_checked_before_extraction(tmp_path: Path) -> None:
    # The unsafe entry must be rejected without writing the safe sibling either.
    zf = _zip([("good.txt", b"ok"), ("../bad.txt", b"x")])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(zf, tmp_path)
    assert not (tmp_path / "good.txt").exists()


def test_empty_zip_is_accepted(tmp_path: Path) -> None:
    zf = _zip([])
    safe_extract(zf, tmp_path)  # no exception
