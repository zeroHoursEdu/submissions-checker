"""On-demand subject trees for the check sandbox.

The subject's config archive lives in Postgres (``subject_plugin_configs.zip_data``); the
sandbox needs it as a directory the host Docker daemon can bind-mount. Trees are unpacked
under a root that has the same path inside the app container and on the host (``/tmp`` in
every compose file), keyed by the archive's hash, so a new config version never mutates a
tree a running check is reading.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from submissions_checker.utils.safe_zip import safe_extract

_TMP_PREFIX = ".tmp-"


def materialize_plugin_tree(
    root: Path, subject_code: str, zip_bytes: bytes, *, max_idle_seconds: int = 86400
) -> Path:
    """Extract a subject's config archive to a hash-keyed directory.

    Returns the path to the extracted tree, creating it on demand. Reuses the same
    directory for identical archives (same subject_code + zip content hash). Prunes
    old versions and stale temp directories that have been idle for max_idle_seconds.

    Raises UnsafeArchiveError if the archive contains path traversal or other unsafe entries.
    """
    digest = hashlib.sha256(zip_bytes).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{subject_code}-{digest}"
    if not target.is_dir():
        tmp = root / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                safe_extract(zf, tmp)
            _widen_permissions(tmp)
            try:
                os.replace(tmp, target)
            except OSError:
                # Another replica renamed its copy into place first; theirs is identical.
                if not target.is_dir():
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    os.utime(target)
    _prune_idle(root, subject_code, keep=target, max_idle_seconds=max_idle_seconds)
    return target


def _widen_permissions(tree: Path) -> None:
    """Set permissions so non-root users in the sandbox can read the tree.

    Directories: 0o755, files: 0o644.
    """
    os.chmod(tree, 0o755)
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames:
            os.chmod(Path(dirpath) / name, 0o755)
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o644)


def _prune_idle(root: Path, subject_code: str, *, keep: Path, max_idle_seconds: int) -> None:
    """Remove this subject's other versions and abandoned temp dirs once nobody used them
    for ``max_idle_seconds`` — never eagerly: a pinned older version may be mid-check on
    another replica sharing this root."""
    cutoff = time.time() - max_idle_seconds
    for entry in root.iterdir():
        if entry == keep or not entry.is_dir():
            continue
        is_tmp = entry.name.startswith(_TMP_PREFIX)
        code, _, digest = entry.name.rpartition("-")
        is_version = code == subject_code and len(digest) == 16
        if not (is_tmp or is_version):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except FileNotFoundError:
            continue
