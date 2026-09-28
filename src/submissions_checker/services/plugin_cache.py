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
import posixpath
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from submissions_checker.utils.safe_zip import safe_extract

_TMP_PREFIX = ".tmp-"
_OLD_PREFIX = ".old-"


def materialize_plugin_tree(
    root: Path, subject_code: str, zip_bytes: bytes, *, max_idle_seconds: int = 86400
) -> Path:
    """Extract a subject's config archive to a hash-keyed directory.

    Returns the path to the extracted tree, creating it on demand. Reuses the same
    directory for identical archives (same subject_code + zip content hash), but only
    when it still has every file the archive says it should — a host `/tmp` cleaner
    (e.g. systemd-tmpfiles on Oracle Linux) can remove individual unread files without
    touching the directory's mtime, which would otherwise hand the sandbox a tree
    silently missing files. Prunes old versions and stale temp/backup directories that
    have been idle for max_idle_seconds.

    Raises UnsafeArchiveError if the archive contains path traversal or other unsafe entries.
    """
    digest = hashlib.sha256(zip_bytes).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{subject_code}-{digest}"
    if not (target.is_dir() and _is_complete(target, zip_bytes)):
        _reextract(root, target, zip_bytes)
    try:
        os.utime(target)
    except FileNotFoundError:
        # Idle-pruned by another replica between the completeness check above and here.
        _reextract(root, target, zip_bytes)
        os.utime(target)
    _prune_idle(root, subject_code, keep=target, max_idle_seconds=max_idle_seconds)
    return target


def _extracted_relpath(filename: str) -> str | None:
    """Where `safe_extract`'s underlying `zipfile.extractall` actually writes this
    entry, relative to the tree root — or None when nothing is written for it.

    This mirrors `zipfile.ZipFile._extract_member`'s own sanitisation (split on '/',
    drop empty/'.'/'..' components, rejoin) rather than `posixpath.normpath`: for an
    entry like "a/../b.txt", normpath mathematically resolves it down to "b.txt", but
    the stdlib's own extraction merely drops the ".." *token* and keeps "a", writing to
    "a/b.txt" instead (confirmed against the stdlib — safe_extract's own traversal
    check happens to accept this entry, since `(dest / "a/../b.txt").resolve()` also
    lands inside dest, just not at the same place extractall actually writes it). A
    completeness check has to agree with reality, not with a plausible-looking but
    different normalisation, or it will consider the tree eternally incomplete.
    """
    parts = [p for p in filename.split("/") if p not in ("", posixpath.curdir, posixpath.pardir)]
    return "/".join(parts) if parts else None


def _is_complete(target: Path, zip_bytes: bytes) -> bool:
    """True when every file entry of the archive still exists under target."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            relpath = _extracted_relpath(info.filename)
            if relpath is None:
                continue
            if not (target / relpath).is_file():
                return False
    return True


def _reextract(root: Path, target: Path, zip_bytes: bytes) -> None:
    """Extract into a fresh temp dir and swap it in for target.

    Re-checks completeness immediately before touching `target`: another replica may
    have already re-extracted a complete tree into it while we were preparing ours, in
    which case we keep theirs and discard our tmp rather than clobbering it. Any
    existing target that IS moved aside lands at `.old-<uuid>` and is left there for the
    idle prune, not removed here — a sandbox on another replica may still be reading it
    through its own bind mount of the old path. Races with another replica doing this
    same dance are tolerated: whichever tree ends up at `target` is equally correct, so
    a losing os.replace just keeps theirs instead of erroring.
    """
    tmp = root / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            safe_extract(zf, tmp)
        _widen_permissions(tmp)
        if target.is_dir() and _is_complete(target, zip_bytes):
            return  # another replica already fixed it; theirs is as good as ours
        try:
            target.rename(root / f"{_OLD_PREFIX}{uuid.uuid4().hex}")
        except FileNotFoundError:
            pass  # nothing to move aside — already gone (raced or never existed)
        try:
            os.replace(tmp, target)
        except OSError:
            # Another replica's freshly (re-)extracted tree is already at target; theirs
            # is as good as ours.
            if not target.is_dir():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    """Remove this subject's other versions and abandoned temp/backup dirs once nobody
    used them for ``max_idle_seconds`` — never eagerly: a pinned older version may be
    mid-check on another replica sharing this root."""
    cutoff = time.time() - max_idle_seconds
    for entry in root.iterdir():
        if entry == keep or not entry.is_dir():
            continue
        is_scratch = entry.name.startswith(_TMP_PREFIX) or entry.name.startswith(_OLD_PREFIX)
        code, _, digest = entry.name.rpartition("-")
        is_version = code == subject_code and len(digest) == 16
        if not (is_scratch or is_version):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except FileNotFoundError:
            continue
