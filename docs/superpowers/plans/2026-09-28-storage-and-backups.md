# MinIO-only Storage and Drive Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Submission ZIPs and subject trees stop living on local disk (MinIO + Postgres become the only stores), and both stores are backed up every 6 hours to Google Drive with a one-command restore.

**Architecture:** A single read/write seam (`services/submission_files.py`) moves submission ZIPs to MinIO with a legacy local-disk fallback; subject trees are unpacked on demand from `subject_plugin_configs.zip_data` into a hash-keyed cache under `/tmp`. The `backup` compose service becomes opt-in (profile), uses rclone for both Drive and MinIO, runs on cron, and also performs restores. Host-side ops scripts wrap it; Makefile targets and Claude skills call the scripts.

**Tech Stack:** FastAPI, SQLAlchemy async, aioboto3/botocore, pytest; alpine + busybox crond + rclone + postgresql16-client; bash.

**Spec:** `docs/superpowers/specs/2026-09-28-storage-and-backups-design.md`

## Global Constraints

- Branch: `storage-to-minio-and-drive-backups`. Merge to `main` and push only after Task 16.
- Always `uv run --frozen …` (never rewrite `uv.lock`). Tests: `uv run --frozen --extra dev pytest -q`.
- Repo is clean at HEAD for tests, ruff, mypy — any failure is ours.
- Object key for submissions: `submissions/<saved_as>`; `saved_as` format unchanged (`<sa_id>_<uuid>.zip`).
- Plugin cache root setting `plugin_cache_dir`, default `"/tmp/subchk-plugins"`; dir name `<subjectCode>-<sha256(zip_data)[:16]>`; idle prune after 86400 s.
- Missing-archive validation message (exact): `Subject config has no stored archive — re-apply the subject config.`
- Backups: `BACKUP_CRON` default `0 */6 * * *`, `TZ` default `Europe/Kyiv`, `BACKUP_RETENTION_DAYS` default `30`. Remote layout: `postgres/<stamp>.dump`, `minio/current/`, `minio/deleted/<stamp>/`, `pre-restore/<stamp>.dump`, `last-success`. Stamp format `%Y%m%dT%H%M%SZ` (UTC).
- Backup service is behind compose profile `backup`; profiled env uses `:-`, never `:?`.
- Drive copies are unencrypted (owner decision D4).
- Skill = script name: `connect-to-prod-db`, `run-prod-backup`, `restore-prod-from-backup` (last one `disable-model-invocation: true`, no Make target).
- No prod host address in git; scripts read `PROD_SSH` / `PROD_DIR` from env or gitignored `ssh/prod.env`.
- Commits: imperative subject ≤72 chars, body explains why, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A `saved_as` containing `/`, `\`, `..` or empty must never reach S3 or the filesystem — `submission_key` raises, readers return `None` (pinned in Task 2).
2. ZIP gone from both MinIO and local disk → check fails validation with a message, teacher download 404s, similarity skips it — never a 500 (Tasks 4, 9).
3. Two replicas materializing the same subject at once → both get a complete tree, neither crashes (Task 5 `test_concurrent_loser_keeps_winner_tree`).
4. A backup run that fails part-way must not prune and must not write `last-success`; retention decides by stamp in the name, not file mtime — `rclone --backup-dir` keeps the original object mtime, so an mtime rule would delete a just-removed old object immediately (Task 12 roundtrip script + script review).
5. A failed restore leaves the database unchanged (`pg_restore --single-transaction`) and the app is started again regardless (Task 12, Task 13 trap).

---

### Task 1: Storage primitives for "maybe missing" objects

**Files:**
- Modify: `src/submissions_checker/services/storage.py`
- Create: `tests/storage_fake.py`
- Test: `tests/unit/test_storage.py`

**Interfaces:**
- Produces: `get_storage(settings: Settings) -> StorageService | None`; `StorageService.try_download_bytes(key: str) -> bytes | None`; `StorageService.object_exists(key: str) -> bool`; `tests.storage_fake.FakeStorage` (dict-backed, same async methods: `upload_bytes`, `upload_file`, `download_bytes`, `try_download_bytes`, `object_exists`, `delete_file`; attribute `objects: dict[str, bytes]`).

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_storage.py`, reuse its `_settings` / `_service_with_mock_client`)

```python
from botocore.exceptions import ClientError

from submissions_checker.services.storage import get_storage


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


def test_get_storage_none_without_endpoint() -> None:
    assert get_storage(_settings(s3_endpoint_url=None)) is None


def test_get_storage_builds_service_with_endpoint() -> None:
    with patch("submissions_checker.services.storage.aioboto3.Session"):
        assert isinstance(get_storage(_settings(s3_endpoint_url="http://minio:9000")), StorageService)


async def test_try_download_returns_none_on_missing_key() -> None:
    svc, s3, _calls, _ = _service_with_mock_client(_settings())
    s3.get_object.side_effect = _client_error("NoSuchKey")
    assert await svc.try_download_bytes("submissions/a.zip") is None


async def test_try_download_reraises_other_errors() -> None:
    svc, s3, _calls, _ = _service_with_mock_client(_settings())
    s3.get_object.side_effect = _client_error("AccessDenied")
    with pytest.raises(ClientError):
        await svc.try_download_bytes("k")


async def test_object_exists_true_and_false() -> None:
    svc, s3, _calls, _ = _service_with_mock_client(_settings())
    assert await svc.object_exists("k") is True
    s3.head_object.side_effect = _client_error("404")
    assert await svc.object_exists("k") is False
```
Add `import pytest` at the top if missing.

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/unit/test_storage.py -q` — expect ImportError on `get_storage`.

- [ ] **Step 3: Implement** in `storage.py`:

```python
from botocore.exceptions import ClientError

_MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


def get_storage(settings: Settings) -> StorageService | None:
    """The object store, or None when none is configured.

    Unit and functional tests run without an endpoint; every caller must then fall back
    to its legacy behaviour rather than fail.
    """
    return StorageService(settings) if settings.s3_endpoint_url else None
```
Methods on `StorageService`:

```python
    async def try_download_bytes(self, key: str) -> bytes | None:
        """Like ``download_bytes`` but None when the object does not exist."""
        try:
            return await self.download_bytes(key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise

    async def object_exists(self, key: str) -> bool:
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                    return False
                raise
        return True
```
If mypy reports botocore as untyped, add `# type: ignore[import-untyped]` to that import only.

Create `tests/storage_fake.py`:

```python
"""In-memory stand-in for StorageService, for tests that must not touch S3."""

from __future__ import annotations

from pathlib import Path


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload_bytes(
        self, data: bytes, key: str, content_type: str = "application/octet-stream"
    ) -> str:
        self.objects[key] = data
        return f"fake://{key}"

    async def upload_file(self, local_path: Path, key: str) -> str:
        self.objects[key] = local_path.read_bytes()
        return f"fake://{key}"

    async def download_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def try_download_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def delete_file(self, key: str) -> None:
        self.objects.pop(key, None)
```

- [ ] **Step 4: Run** the test file — PASS. Run `uv run --frozen mypy src/`.
- [ ] **Step 5: Commit** — `Add storage helpers for objects that may be missing` (body: submission ZIPs move to S3 with a local fallback, so readers need a not-found that is not an exception).

---

### Task 2: Submission ZIP read/write seam

**Files:**
- Create: `src/submissions_checker/services/submission_files.py`
- Test: `tests/unit/test_submission_files.py`

**Interfaces:**
- Consumes: `StorageService.upload_bytes`, `try_download_bytes` (Task 1), `FakeStorage`.
- Produces:
  - `LEGACY_UPLOADS_DIR: Path = Path("uploads")`
  - `submission_key(saved_as: str) -> str` (raises `ValueError` on unsafe names)
  - `async store_submission_zip(storage: StorageService | None, saved_as: str, data: bytes, legacy_dir: Path = LEGACY_UPLOADS_DIR) -> None`
  - `async read_submission_zip(storage: StorageService | None, saved_as: str | None, legacy_dir: Path = LEGACY_UPLOADS_DIR) -> bytes | None`

- [ ] **Step 1: Failing tests**

```python
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
```

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/unit/test_submission_files.py -q` — ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run** tests — PASS.
- [ ] **Step 5: Commit** — `Add one seam for reading and writing submission ZIPs`.

---

### Task 3: Similarity accepts bytes

**Files:**
- Modify: `src/submissions_checker/services/similarity.py`
- Test: `tests/unit/test_similarity.py`

**Interfaces:**
- Produces: `ZipSource = Path | bytes`; `compare_zip_files(a: ZipSource, b: ZipSource) -> float`; `token_set_for_zip(src: ZipSource) -> frozenset[str]`.

- [ ] **Step 1: Failing test** (append)

```python
import io
import zipfile

from submissions_checker.services.similarity import compare_zip_files, token_set_for_zip


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_bytes_and_path_give_same_score(tmp_path) -> None:
    a = _zip_bytes({"a.py": "def total(x):\n    return x\n"})
    b = _zip_bytes({"b.py": "def total(y):\n    return y\n"})
    (tmp_path / "a.zip").write_bytes(a)
    assert compare_zip_files(a, b) == compare_zip_files(tmp_path / "a.zip", b)
    assert "total" in token_set_for_zip(a)


def test_garbage_bytes_are_empty_not_error() -> None:
    assert token_set_for_zip(b"not a zip") == frozenset()
```

- [ ] **Step 2: Run** — `test_bytes_and_path_give_same_score` fails (bytes passed to `ZipFile` is treated as a filename → swallowed → 0.0 vs non-zero; or assertion on `"total"` fails).

- [ ] **Step 3: Implement** — add `import io`, define `ZipSource = Path | bytes`, change `_extract_tokens(src: ZipSource)` to open `zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src)`, and retype `compare_zip_files(path_a: ZipSource, path_b: ZipSource)` / `token_set_for_zip(zip_path: ZipSource)`. Docstrings: "a ZIP path or its bytes".

- [ ] **Step 4: Run** `tests/unit/test_similarity.py` — PASS.
- [ ] **Step 5: Commit** — `Let similarity read ZIPs from memory` (body: ZIPs now come from object storage as bytes).

---

### Task 4: Check worker reads ZIPs through the seam

**Files:**
- Modify: `src/submissions_checker/workers/tasks/check_tasks.py` (~lines 163-175 and `_accept_without_checks` ~274-300; call site ~126)
- Test: `tests/integration/test_worker_tasks.py`, `tests/unit/test_check_task_delegation.py`

**Interfaces:**
- Consumes: `read_submission_zip` (Task 2), `get_storage` (Task 1).
- Produces: `async def _accept_without_checks(submission: Submission, review_mode: str) -> None` (now async); module keeps `UPLOADS_DIR` (tests monkeypatch it; it is passed as `legacy_dir`).

- [ ] **Step 1: Failing tests** (append to `tests/integration/test_worker_tasks.py`, next to `test_check_validation_failed`)

```python
@pytest.mark.asyncio
async def test_check_reads_zip_from_object_storage(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    """With an object store configured the worker reads the ZIP from it, not from disk."""
    from tests.storage_fake import FakeStorage

    zip_path = tmp_path / "src.zip"
    _write_zip(zip_path)
    storage = FakeStorage()
    storage.objects["submissions/s3.zip"] = zip_path.read_bytes()
    sub = await _seed_check_submission(db_session, "s3", saved_as="s3.zip")

    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path / "empty")
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    monkeypatch.setattr(check_tasks, "get_storage", lambda _s: storage)
    _patch_run_check(monkeypatch, check_core.CheckOutcome("passed", 1, 1, []))

    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id})
    await _process(db_session, monkeypatch, message)

    await db_session.refresh(sub)
    assert sub.status != SubmissionStatus.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_check_zip_missing_everywhere_fails_validation(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    sub = await _seed_check_submission(db_session, "gone", saved_as="gone.zip")
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(monkeypatch, check_core.CheckOutcome("passed", 1, 1, []))

    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id})
    message = await _process(db_session, monkeypatch, message)

    assert message.state == OutboxMessageState.FINISHED
    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert "Could not open submitted ZIP" in sub.test_results["check_reason"]
```
(Check the exact `CheckOutcome` positional args against `check_core.CheckOutcome` and existing uses in this file; match them.)

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/integration/test_worker_tasks.py -q -k "object_storage or missing_everywhere"` — first fails with `AttributeError: ... has no attribute 'get_storage'`.

- [ ] **Step 3: Implement**
  - Imports: `import io`; `from submissions_checker.services.storage import get_storage`; `from submissions_checker.services.submission_files import read_submission_zip`.
  - Main path: replace `zip_path = UPLOADS_DIR / saved_as` and the `zipfile.ZipFile(zip_path, "r")` block with:

```python
    zip_bytes = await read_submission_zip(get_storage(settings), saved_as, UPLOADS_DIR)
    if zip_bytes is None:
        _fail_validation(submission, "Could not open submitted ZIP: file not found in storage")
        return

    with tempfile.TemporaryDirectory(prefix="submission_") as extract_dir:
        extract_path = Path(extract_dir)
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                safe_extract(zf, extract_path)
```
  (keep the existing `except` branches unchanged).
  - `_accept_without_checks` → `async def`; read with `await read_submission_zip(get_storage(get_settings()), saved_as, UPLOADS_DIR)`, same `None` → `_fail_validation(...)`/`return`, and `zipfile.ZipFile(io.BytesIO(zip_bytes), "r")`. Keep its `RuntimeError` for a missing `saved_as` as today.
  - Call site: `await _accept_without_checks(submission, review_mode)`. `grep -n "_accept_without_checks" -r src tests` and await every call.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/integration/test_worker_tasks.py tests/unit/test_check_task_delegation.py tests/functional/test_quiz_first_and_stepper.py -q` — all PASS (existing tests still monkeypatch `UPLOADS_DIR` with no storage → fallback path).
- [ ] **Step 5: Commit** — `Read submission ZIPs from object storage in the check worker`.

---

### Task 5: Plugin tree cache

**Files:**
- Create: `src/submissions_checker/services/plugin_cache.py`
- Modify: `src/submissions_checker/core/config.py` (next to `plugins_dir`)
- Test: `tests/unit/test_plugin_cache.py`

**Interfaces:**
- Produces: `materialize_plugin_tree(root: Path, subject_code: str, zip_bytes: bytes, *, max_idle_seconds: int = 86400) -> Path`; `Settings.plugin_cache_dir: str = "/tmp/subchk-plugins"`.

- [ ] **Step 1: Failing tests**

```python
"""Subject trees unpacked on demand from the config archive stored in Postgres."""

from __future__ import annotations

import io
import os
import stat
import time
import zipfile

import pytest

from submissions_checker.services import plugin_cache
from submissions_checker.services.plugin_cache import materialize_plugin_tree
from submissions_checker.utils.safe_zip import UnsafeArchiveError


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_extracts_tree_named_by_code_and_hash(tmp_path) -> None:
    z = _zip({"config.yml": "subjectCode: demo\n", "assignments/lab1/check.py": "x"})
    out = materialize_plugin_tree(tmp_path, "demo", z)
    assert out.parent == tmp_path
    assert out.name.startswith("demo-") and len(out.name) == len("demo-") + 16
    assert (out / "assignments/lab1/check.py").read_text() == "x"


def test_same_archive_reuses_directory(tmp_path) -> None:
    z = _zip({"a.txt": "1"})
    first = materialize_plugin_tree(tmp_path, "demo", z)
    (first / "marker").write_text("kept")
    assert materialize_plugin_tree(tmp_path, "demo", z) == first
    assert (first / "marker").exists()


def test_files_readable_by_non_root_sandbox(tmp_path) -> None:
    out = materialize_plugin_tree(tmp_path, "demo", _zip({"d/f.py": "x"}))
    assert stat.S_IMODE((out / "d").stat().st_mode) == 0o755
    assert stat.S_IMODE((out / "d/f.py").stat().st_mode) == 0o644


def test_old_version_kept_while_recently_used(tmp_path) -> None:
    old = materialize_plugin_tree(tmp_path, "demo", _zip({"v": "1"}))
    materialize_plugin_tree(tmp_path, "demo", _zip({"v": "2"}))
    assert old.is_dir()  # another replica may still be checking against it


def test_idle_old_version_and_stale_tmp_pruned(tmp_path) -> None:
    old = materialize_plugin_tree(tmp_path, "demo", _zip({"v": "1"}))
    other_subject = materialize_plugin_tree(tmp_path, "demo2", _zip({"v": "1"}))
    stale_tmp = tmp_path / ".tmp-deadbeef"
    stale_tmp.mkdir()
    past = time.time() - 2 * 86400
    for p in (old, other_subject, stale_tmp):
        os.utime(p, (past, past))
    materialize_plugin_tree(tmp_path, "demo", _zip({"v": "2"}))
    assert not old.exists()
    assert not stale_tmp.exists()
    assert other_subject.is_dir()  # other subjects are not this call's business


def test_concurrent_loser_keeps_winner_tree(tmp_path, monkeypatch) -> None:
    z = _zip({"a.txt": "winner"})
    real_replace = os.replace

    def racing_replace(src, dst):
        # Another replica finishes first: the target appears just before our rename.
        if not os.path.exists(dst):
            os.makedirs(dst)
            with open(os.path.join(dst, "a.txt"), "w") as f:
                f.write("winner")
        real_replace(src, dst)

    monkeypatch.setattr(plugin_cache.os, "replace", racing_replace)
    out = materialize_plugin_tree(tmp_path, "demo", z)
    assert (out / "a.txt").read_text() == "winner"
    assert not any(p.name.startswith(".tmp-") for p in tmp_path.iterdir())


def test_unsafe_archive_leaves_nothing(tmp_path) -> None:
    with pytest.raises(UnsafeArchiveError):
        materialize_plugin_tree(tmp_path, "demo", _zip({"../evil": "x"}))
    assert [p for p in tmp_path.iterdir()] == []
```

- [ ] **Step 2: Run** — ModuleNotFoundError.

- [ ] **Step 3: Implement** `plugin_cache.py`:

```python
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
    digest = hashlib.sha256(zip_bytes).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{subject_code}-{digest}"
    if not target.is_dir():
        tmp = root / f"{_TMP_PREFIX}{uuid.uuid4().hex}"
        try:
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
    # Subject images run checks as a non-root user; archive mode bits must not lock it out.
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
```
Check `safe_extract` behaviour: if it raises before creating `tmp`, the `finally` rmtree is a no-op — fine. If `safe_extract` requires the target to exist, create `tmp` first with `tmp.mkdir()`.

`config.py`, under `host_plugins_dir`:

```python
    # Where check workers unpack a subject's stored config archive for the sandbox. Must be
    # the same path inside the app container and on the host (the daemon resolves bind
    # mounts on the host) — /tmp is mounted that way in every compose file.
    plugin_cache_dir: str = "/tmp/subchk-plugins"
```

- [ ] **Step 4: Run** `tests/unit/test_plugin_cache.py` — PASS; mypy.
- [ ] **Step 5: Commit** — `Unpack subject trees on demand from the stored config archive`.

---

### Task 6: Check worker resolves the subject tree from Postgres

**Files:**
- Modify: `src/submissions_checker/workers/tasks/check_tasks.py` (~lines 154-162)
- Test: `tests/integration/test_worker_tasks.py`, `tests/unit/test_check_task_delegation.py`

**Interfaces:**
- Consumes: `materialize_plugin_tree`, `Settings.plugin_cache_dir` (Task 5).
- Produces: `MISSING_ARCHIVE_REASON: str` constant; `_resolve_plugin_dir(settings: Settings, subject_code: str, zip_data: bytes | None) -> Path | None`.

- [ ] **Step 1: Failing tests** (integration file). Add helper + tests:

```python
def _plugin_zip() -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.yml", "subjectCode: sub\n")
        zf.writestr("check.py", "print('ok')\n")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_check_uses_tree_unpacked_from_zip_data(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    zip_path = tmp_path / "z.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "zd", saved_as="z.zip", zip_data=_plugin_zip())
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(test_settings, "plugin_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    seen: dict = {}

    async def fake_run_check(*, plan, submission_dir, plugin_dir, sandbox):
        seen["plugin_dir"] = plugin_dir
        seen["has_check"] = (plugin_dir / "check.py").is_file()
        return check_core.CheckOutcome("passed", 1, 1, [])

    monkeypatch.setattr(check_tasks.check_core, "run_check", fake_run_check)
    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id})
    await _process(db_session, monkeypatch, message)

    assert seen["plugin_dir"].parent == tmp_path / "cache"
    assert seen["has_check"] is True


@pytest.mark.asyncio
async def test_check_without_zip_data_and_no_legacy_tree_fails(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    zip_path = tmp_path / "n.zip"
    _write_zip(zip_path)
    sub = await _seed_check_submission(db_session, "nz", saved_as="n.zip", zip_data=None)
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(test_settings, "plugins_dir", str(tmp_path / "no-plugins"))
    monkeypatch.setattr(test_settings, "host_plugins_dir", None)
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    _patch_run_check(monkeypatch, check_core.CheckOutcome("passed", 1, 1, []))

    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id})
    await _process(db_session, monkeypatch, message)

    await db_session.refresh(sub)
    assert sub.status == SubmissionStatus.VALIDATION_FAILED
    assert sub.test_results["check_reason"] == check_tasks.MISSING_ARCHIVE_REASON


@pytest.mark.asyncio
async def test_check_without_zip_data_uses_legacy_tree(
    db_session: AsyncSession, test_settings, monkeypatch, tmp_path
) -> None:
    zip_path = tmp_path / "l.zip"
    _write_zip(zip_path)
    (tmp_path / "plugins" / "sub").mkdir(parents=True)
    sub = await _seed_check_submission(db_session, "lg", saved_as="l.zip", zip_data=None)
    monkeypatch.setattr(check_tasks, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(test_settings, "plugins_dir", str(tmp_path / "plugins"))
    monkeypatch.setattr(test_settings, "host_plugins_dir", "/host/plugins")
    monkeypatch.setattr(check_tasks, "get_settings", lambda: test_settings)
    seen: dict = {}

    async def fake_run_check(*, plan, submission_dir, plugin_dir, sandbox):
        seen["plugin_dir"] = plugin_dir
        return check_core.CheckOutcome("passed", 1, 1, [])

    monkeypatch.setattr(check_tasks.check_core, "run_check", fake_run_check)
    message = OutboxMessage(event_type=OutboxEventType.RUN_CHECKS, payload={"submission_id": sub.id})
    await _process(db_session, monkeypatch, message)

    assert seen["plugin_dir"] == Path("/host/plugins/sub")
```
Extend `_seed_check_submission` with a keyword `zip_data: bytes | None = _DEFAULT` where the default is `_plugin_zip()` (define `_plugin_zip` above it; use a sentinel so `None` can be passed explicitly), and pass `zip_data=` into `SubjectPluginConfig(...)`. Ensure `Path` is imported in the test module.

In `tests/unit/test_check_task_delegation.py`: the fake `config_record` namespaces gain `zip_data=<small valid zip bytes>` and each test monkeypatches the settings used by `check_tasks.get_settings` so `plugin_cache_dir=str(tmp_path / "cache")`. Follow however that file already overrides settings (look at lines 105-130).

- [ ] **Step 2: Run** the three new tests — they fail (no `MISSING_ARCHIVE_REASON`, and plugin_dir is still `plugins/sub`).

- [ ] **Step 3: Implement** in `check_tasks.py`:

```python
MISSING_ARCHIVE_REASON = "Subject config has no stored archive — re-apply the subject config."


def _resolve_plugin_dir(settings: Settings, subject_code: str, zip_data: bytes | None) -> Path | None:
    """Host path of the subject tree the sandbox mounts, or None when there is none.

    The stored archive is the source of truth. Configs applied before migration 0018 have
    no archive; for those the legacy extracted tree under plugins_dir is still honoured
    (checked via the container path, mounted from the host path).
    """
    if zip_data:
        return materialize_plugin_tree(Path(settings.plugin_cache_dir), subject_code, zip_data)
    if (Path(settings.plugins_dir) / subject_code).is_dir():
        return Path(settings.host_plugins_dir or settings.plugins_dir) / subject_code
    return None
```
Replace `plugins_root = …` / `plugin_dir = Path(plugins_root) / subject_code` with (after `subject_code` is validated):

```python
    plugin_dir = await asyncio.to_thread(
        _resolve_plugin_dir, settings, subject_code, getattr(config_record, "zip_data", None)
    )
    if plugin_dir is None:
        _fail_validation(submission, MISSING_ARCHIVE_REASON)
        return
```
Imports: `import asyncio`, `from submissions_checker.core.config import Settings, get_settings`, `from submissions_checker.services.plugin_cache import materialize_plugin_tree`. Catch `UnsafeArchiveError` / `zipfile.BadZipFile` from materialization → `_fail_validation(submission, f"Stored subject archive is unusable: {exc}")`.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/integration/test_worker_tasks.py tests/unit/test_check_task_delegation.py -q` — PASS.
- [ ] **Step 5: Commit** — `Mount subject trees unpacked from Postgres into the sandbox` (body: the plugins volume was a second copy of data Postgres already holds; the legacy tree remains a fallback for pre-0018 configs).

---

### Task 7: Config apply stops writing `plugins/`

**Files:**
- Modify: `src/submissions_checker/services/config_apply.py` (constructor ~92, dedup self-heal ~296, `_extract_plugin_tree` ~306-330 and its call site, module docstring lines 1-10)
- Modify: `src/submissions_checker/api/routes/teacher_portal.py:158`
- Modify tests: `tests/integration/test_config_apply.py`, `tests/integration/test_config_apply_edges.py`, `tests/unit/test_config_apply_helpers.py`

**Interfaces:**
- Produces: `ConfigApplyService(storage: StorageService | None)` — `plugins_dir` parameter removed.

- [ ] **Step 1: Test first.** In `tests/integration/test_config_apply.py` delete the tests that assert extraction (`test_fresh_apply_extracts_full_zip_tree_to_plugins_dir`, `test_reapply_removes_stale_files_from_plugins_dir`, and the self-heal test around line 561 — read each; delete those whose subject is the on-disk tree). Add:

```python
async def test_apply_writes_nothing_to_local_disk(db_session, tmp_path, monkeypatch) -> None:
    """The archive in Postgres is the only copy; workers unpack it on demand."""
    monkeypatch.chdir(tmp_path)
    svc = ConfigApplyService(storage=None)
    await svc.apply(_zip_bytes(), owner_id=<owner id as other tests obtain it>, db=db_session)
    assert list(tmp_path.iterdir()) == []
```
Use the same zip builder / owner fixture the neighbouring tests use (read the file head). Then mechanically drop the argument everywhere:
`sed -i 's/, plugins_dir=tmp_path)/)/; s/ConfigApplyService(storage=None, plugins_dir=[^)]*)/ConfigApplyService(storage=None)/' tests/integration/test_config_apply.py tests/integration/test_config_apply_edges.py tests/unit/test_config_apply_helpers.py` and `grep -rn "plugins_dir" tests/integration/test_config_apply*.py tests/unit/test_config_apply_helpers.py` → fix any left by hand.

- [ ] **Step 2: Run** `uv run --frozen --extra dev pytest tests/integration/test_config_apply.py -q` — TypeError on the constructor.

- [ ] **Step 3: Implement** — remove `plugins_dir` param and `self._plugins_dir`; delete `_extract_plugin_tree` and its call; in the dedup branch drop the `is_dir()` self-heal (return the `ApplyResult` directly; remove the docstring sentences about self-heal); update the module docstring order line to `S3 uploads → DB transaction → S3 cleanup` and say the archive in `subject_plugin_configs.zip_data` is what checks unpack. Remove now-unused imports (`shutil`, `os`, `uuid`, `safe_extract`, `UnsafeArchiveError`) only if nothing else uses them (ruff will tell). Route: `service = ConfigApplyService(get_storage(settings))` (import `get_storage`; `Path` import may become unused).

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/integration/test_config_apply.py tests/integration/test_config_apply_edges.py tests/unit/test_config_apply_helpers.py tests/functional/test_apply_config.py -q` + `ruff check src tests` — PASS.
- [ ] **Step 5: Commit** — `Stop extracting subject configs into the plugins directory`.

---

### Task 8: Student upload stores the ZIP in MinIO

**Files:**
- Modify: `src/submissions_checker/api/routes/student_portal.py` (imports; `UPLOADS_DIR` lines 59-60; `submit_assignment` 498-625)
- Test: `tests/functional/test_student_portal.py`

**Interfaces:**
- Consumes: `store_submission_zip`, `read_submission_zip`, `get_storage`, `compare_zip_files(bytes, bytes)`.

- [ ] **Step 1: Failing test.** Read the existing ZIP-submit test in `tests/functional/test_student_portal.py` (grep `saved_as` / `.zip`) and copy its arrange section. New test:

```python
async def test_submit_stores_zip_in_object_storage(<same fixtures as the existing submit test>) -> None:
    from submissions_checker.api.routes import student_portal as student_portal_module
    from tests.storage_fake import FakeStorage

    storage = FakeStorage()
    with patch.object(student_portal_module, "get_storage", return_value=storage):
        <perform the same POST as the existing submit test>
    sub = <load the created Submission as that test does>
    key = f"submissions/{sub.source_metadata['saved_as']}"
    assert key in storage.objects
    assert not (student_portal_module.UPLOADS_DIR / sub.source_metadata["saved_as"]).exists()
```
And a similarity test: pre-seed a classmate's submission whose ZIP exists only in `storage.objects` with identical content; after upload, `similarity_score == 1.0`.

- [ ] **Step 2: Run** — fails (`get_storage` not in module / object missing).

- [ ] **Step 3: Implement**
  - Remove `UPLOADS_DIR.mkdir(exist_ok=True)`; keep `UPLOADS_DIR = Path("uploads")` (legacy fallback dir passed to the seam).
  - Add `settings: AppSettings` to `submit_assignment`'s parameters; `storage = get_storage(settings)`.
  - Replace the `aiofiles` write with `await store_submission_zip(storage, save_name, content, UPLOADS_DIR)` — before `db.add(submission)` (object first, row second).
  - Similarity loop:

```python
        other_name = meta.get("saved_as") if meta else None
        other = await read_submission_zip(storage, other_name, UPLOADS_DIR)
        if other is not None:
            max_similarity = max(max_similarity, compare_zip_files(content, other))
```
  - Drop `aiofiles` import if unused.

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/functional/test_student_portal.py tests/functional/test_squad_submission.py -q` — PASS.
- [ ] **Step 5: Commit** — `Store uploaded submission ZIPs in object storage` (body: the uploads volume was the one store no backup covered).

---

### Task 9: Teacher similarity report and ZIP download read from MinIO

**Files:**
- Modify: `src/submissions_checker/api/routes/teacher_portal.py` (`teacher_similarity_report` ~648-740, `teacher_download_submission` ~1305-1350)
- Test: `tests/functional/test_teacher_portal.py`

- [ ] **Step 1: Failing tests.** Locate existing download / similarity tests (`grep -n "download\|similarity" tests/functional/test_teacher_portal*.py`). Add, reusing their arrange code:
  - download served from `FakeStorage` (patch `teacher_portal_module.get_storage`) with no local file: 200, body equals stored bytes, `content-disposition` contains the original filename.
  - download with the object nowhere: 404.
  - similarity report with two submissions stored only in `FakeStorage` with identical content: page shows `100`.

- [ ] **Step 2: Run** — fail.

- [ ] **Step 3: Implement**
  - Similarity: collect `blobs: dict[int, bytes]` via `await read_submission_zip(storage, saved_as, UPLOADS_DIR)` (skip `None`), `storage = get_storage(settings)` (add `settings: AppSettings` param if absent); `_compute` uses `token_set_for_zip(b)`; `compared=len(blobs)`; `too_many = len(blobs) > _SIMILARITY_MAX_ITEMS` — check the cap **before** downloading: count candidate rows first and skip downloads when over the cap.
  - Download:

```python
    data = await read_submission_zip(get_storage(settings), saved_as, UPLOADS_DIR)
    if data is None:
        raise HTTPException(status_code=404, detail="Submission file is no longer available")
    original = (submission.source_metadata or {}).get("original_filename") or saved_as
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": _attachment_header(Path(original).name)},
    )
```
  Build `_attachment_header` exactly as Starlette's `FileResponse` does (RFC 5987 `filename*=utf-8''…` for non-ASCII — Ukrainian filenames are common): `from urllib.parse import quote`; `f"attachment; filename*=utf-8''{quote(name)}"` when `name` is not ASCII, else `f'attachment; filename="{name}"'`. Remove the `uploads_root` path-traversal block (the seam's `submission_key` enforces it).

- [ ] **Step 4: Run** `uv run --frozen --extra dev pytest tests/functional/test_teacher_portal.py tests/functional/test_teacher_portal_deep.py -q` — PASS.
- [ ] **Step 5: Commit** — `Serve teacher ZIP downloads and similarity from object storage`.

---

### Task 10: `migrate_uploads` backfill command

**Files:**
- Create: `src/submissions_checker/cli/migrate_uploads.py`
- Test: `tests/integration/test_migrate_uploads.py`

**Interfaces:**
- Produces: `@dataclass MigrationReport(copied: int, present: int, missing: list[int], subjects_without_archive: list[str])`; `async migrate_uploads(db: AsyncSession, storage: StorageService, uploads_dir: Path) -> MigrationReport`; `main() -> int`.

- [ ] **Step 1: Failing test**

```python
"""Backfill of legacy local submission ZIPs into object storage."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.cli.migrate_uploads import migrate_uploads
from tests.integration.test_worker_tasks import _seed_check_submission
from tests.storage_fake import FakeStorage


@pytest.mark.asyncio
async def test_copies_missing_skips_present_reports_absent(db_session: AsyncSession, tmp_path) -> None:
    a = await _seed_check_submission(db_session, "mu-a", saved_as="a.zip")
    b = await _seed_check_submission(db_session, "mu-b", saved_as="b.zip", zip_data=None)
    c = await _seed_check_submission(db_session, "mu-c", saved_as="c.zip")
    (tmp_path / "a.zip").write_bytes(b"A")
    storage = FakeStorage()
    storage.objects["submissions/b.zip"] = b"B"

    report = await migrate_uploads(db_session, storage, tmp_path)

    assert storage.objects["submissions/a.zip"] == b"A"
    assert (report.copied, report.present) == (1, 1)
    assert report.missing == [c.id]
    assert "sub" in report.subjects_without_archive  # b's subject has zip_data NULL

    again = await migrate_uploads(db_session, storage, tmp_path)
    assert (again.copied, again.present) == (0, 2)
```
`_seed_check_submission` must set `source_type=ZIP_UPLOAD` on the submission; if `_seed_submission` does not, pass it through or set `sub.source_type` in the test before commit. Note the query filters must work with other tests' rows in the same DB — scope assertions: filter `report.missing` to `{a.id, b.id, c.id}` if the DB is shared across tests (check how `db_session` isolates; if it rolls back per test, exact equality is fine).

- [ ] **Step 2: Run** — ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
"""One-time backfill: copy legacy local submission ZIPs into object storage.

    docker compose -f docker-compose.prod.yml --env-file .env run --rm app \
        python -m submissions_checker.cli.migrate_uploads

Idempotent — objects already present are skipped, so it is safe to re-run. Also lists
subjects whose latest config predates stored archives (migration 0018): those must be
re-applied before the legacy plugins directory can be removed.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.db.models import Subject, SubjectPluginConfig, Submission
from submissions_checker.db.models.enums import SubmissionSourceType
from submissions_checker.db.session import get_session
from submissions_checker.services.storage import StorageService, get_storage
from submissions_checker.services.submission_files import LEGACY_UPLOADS_DIR, submission_key


@dataclass
class MigrationReport:
    copied: int = 0
    present: int = 0
    missing: list[int] = field(default_factory=list)
    subjects_without_archive: list[str] = field(default_factory=list)


async def migrate_uploads(db: AsyncSession, storage: StorageService, uploads_dir: Path) -> MigrationReport:
    report = MigrationReport()
    rows = await db.execute(
        select(Submission.id, Submission.source_metadata)
        .where(Submission.source_type == SubmissionSourceType.ZIP_UPLOAD)
        .order_by(Submission.id)
    )
    for sub_id, meta in rows:
        saved_as = (meta or {}).get("saved_as")
        try:
            key = submission_key(saved_as or "")
        except ValueError:
            report.missing.append(sub_id)
            continue
        if await storage.object_exists(key):
            report.present += 1
            continue
        path = uploads_dir / saved_as
        if path.is_file():
            await storage.upload_file(path, key)
            report.copied += 1
        else:
            report.missing.append(sub_id)

    latest = (
        select(SubjectPluginConfig.subject_id, func.max(SubjectPluginConfig.version).label("v"))
        .group_by(SubjectPluginConfig.subject_id)
        .subquery()
    )
    codes = await db.scalars(
        select(Subject.code)
        .join(SubjectPluginConfig, SubjectPluginConfig.subject_id == Subject.id)
        .join(latest, (latest.c.subject_id == SubjectPluginConfig.subject_id) & (latest.c.v == SubjectPluginConfig.version))
        .where(SubjectPluginConfig.zip_data.is_(None))
        .order_by(Subject.code)
    )
    report.subjects_without_archive = list(codes)
    return report


async def _run() -> int:
    storage = get_storage(get_settings())
    if storage is None:
        print("S3_ENDPOINT_URL is not set — nothing to migrate into.", file=sys.stderr)
        return 2
    async with get_session() as db:
        report = await migrate_uploads(db, storage, LEGACY_UPLOADS_DIR)
    print(f"copied: {report.copied}")
    print(f"already in storage: {report.present}")
    print(f"missing locally (lost before this migration): {len(report.missing)}")
    if report.missing:
        print("  submission ids: " + ", ".join(map(str, report.missing)))
    if report.subjects_without_archive:
        print("subjects to re-apply (no stored archive): " + ", ".join(report.subjects_without_archive))
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
```
Verify the subject code column name (`grep -n "code" src/submissions_checker/db/models/subject.py`); if the subject's code is only in `config["subjectCode"]`, select `SubjectPluginConfig.config["subjectCode"].astext` instead and adjust the test. Also check whether `get_session` requires the engine to be initialised (look at `core/database.py`: if `get_session_factory` needs `init_db`, call it in `_run`).

- [ ] **Step 4: Run** the test — PASS; mypy.
- [ ] **Step 5: Commit** — `Add a backfill command for legacy local submission ZIPs`.

---

### Task 11: Compose — MinIO everywhere, plugins mount optional

**Files:**
- Modify: `docker-compose.yml`, `docker-compose.e2e.yml`, `docker-compose.prod.yml` (app service only in this task)
- Delete: `docker/localstack/` (after grep shows no other users: `grep -rn localstack --exclude-dir=.git .`; update README/docs hits)

- [ ] **Step 1: Dev compose.** Replace the `localstack` service with:

```yaml
  minio:
    image: quay.io/minio/minio:latest
    container_name: submissions-checker-minio
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "9000:9000"
      - "9001:9001"   # console: http://localhost:9001 (minioadmin / minioadmin)
    volumes:
      - minio_data:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 10

  minio-init:
    image: quay.io/minio/mc:latest
    restart: "no"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
      S3_BUCKET_NAME: submissions-checker
    entrypoint: ["/bin/sh", "/init.sh"]
    volumes:
      - ./docker/minio/init.sh:/init.sh:ro
    depends_on:
      minio:
        condition: service_healthy
```
App env: `S3_ENDPOINT_URL=http://minio:9000`, `AWS_ACCESS_KEY_ID=minioadmin`, `AWS_SECRET_ACCESS_KEY=minioadmin`; remove `S3_PUBLIC_BASE_URL` (no template renders public object URLs — verified with `grep -rn picture templates`). `depends_on`: `minio: service_healthy`, `minio-init: service_completed_successfully`. Add `minio_data:` and `postgres_data:` under `volumes:` (postgres_data is declared but not mounted today — mount it: `postgres: volumes: - postgres_data:/var/lib/postgresql/data`, otherwise dev DB dies with the container; it is declared already, so this is the intent).
Keep `./plugins:/app/plugins` and `HOST_PLUGINS_DIR` for now (legacy fallback) with a comment `# legacy: only for configs applied before migration 0018; remove with the uploads fallback`.

- [ ] **Step 2: e2e compose.** Same swap, names `minio-e2e` / `minio-init-e2e`, no host ports, `S3_ENDPOINT_URL=http://minio-e2e:9000`, drop `S3_PUBLIC_BASE_URL`, drop the `e2e_uploads` volume and its mount (uploads now go to MinIO), no named data volume (ephemeral is right for e2e).

- [ ] **Step 3: Prod compose app service.**
  - `HOST_PLUGINS_DIR: "${HOST_PLUGINS_DIR:-}"` and mount `- ${HOST_PLUGINS_DIR:-./plugins}:/app/plugins`; rewrite the comment block above it: the app no longer writes here; kept read-only-in-practice for configs applied before 0018; removed in phase 2.
  - Keep `uploads:/app/uploads` with comment: legacy read-only fallback until `migrate_uploads` reports 0 missing; phase 2 removes it.
  - Add `PLUGIN_CACHE_DIR: /tmp/subchk-plugins` explicitly (documents the `/tmp` same-path requirement next to the `/tmp:/tmp` mount).

- [ ] **Step 4: Verify.**
  - `docker compose config -q && docker compose -f docker-compose.e2e.yml config -q && HOST_PLUGINS_DIR= docker compose -f docker-compose.prod.yml --env-file .env.example config -q` (create a throwaway env file in the scratchpad with the `:?`-required vars if `.env.example` lacks them).
  - Check which checkout the running dev stack comes from (see CLAUDE.md), then `make down && make up`, open http://localhost:8000, log in as the dev teacher, apply `plugins/e2e_test` zipped config, submit a ZIP as a student, confirm the object in the MinIO console under `submissions/` and the check completes.
  - `make e2e` — all scenarios PASS.

- [ ] **Step 5: Commit** — `Run dev and e2e on MinIO and relax the plugins mount` (body: dev must exercise the same private-bucket code path as prod; localstack's public-read bucket hid bugs).

---

### Task 12: Backup service — opt-in, rclone, cron, restore

**Files:**
- Rewrite: `docker/backup/Dockerfile`, `docker/backup/backup.sh`
- Modify: `docker-compose.prod.yml` (backup service, memory budget header)
- Create: `docker-compose.backup-test.yml`, `tests/ops/backup_roundtrip.sh`
- Modify: `.gitignore` (add `docker/backup/rclone/`)

- [ ] **Step 1: Write the roundtrip test first** — `tests/ops/backup_roundtrip.sh` (executable):

```bash
#!/usr/bin/env bash
# End-to-end proof that a backup can be restored: seed → back up → destroy → restore → assert.
# Runs an isolated compose project (own volumes, no host ports); never touches the dev stack.
set -euo pipefail
cd "$(dirname "$0")/../.."

export COMPOSE_PROJECT_NAME=subchk-backup-test
dc() { docker compose -f docker-compose.backup-test.yml "$@"; }
psql_() { dc exec -T postgres psql -U postgres -d app -tAc "$1"; }
cleanup() { dc down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

dc build backup
dc up -d --wait postgres minio
dc run --rm minio-init

psql_ "CREATE TABLE marker(v text); INSERT INTO marker VALUES ('before');"
dc run --rm --entrypoint sh backup -c 'echo hello | rclone rcat minio:submissions-checker/roundtrip/marker.txt'

dc run --rm backup once
dc run --rm backup status

psql_ "DROP TABLE marker;"
dc run --rm --entrypoint sh backup -c 'rclone deletefile minio:submissions-checker/roundtrip/marker.txt'

dc run --rm -e RESTORE_CONFIRM=yes backup restore latest

test "$(psql_ 'SELECT v FROM marker;')" = "before"
test "$(dc run --rm --entrypoint sh backup -c 'rclone cat minio:submissions-checker/roundtrip/marker.txt')" = "hello"
dc run --rm --entrypoint sh backup -c 'test -n "$(rclone lsf "$RCLONE_REMOTE/pre-restore")"'

# Refuses to restore without confirmation.
if dc run --rm backup restore latest; then echo "restore ran without RESTORE_CONFIRM"; exit 1; fi

# A failed run neither prunes nor writes last-success: break postgres and compare.
before="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
dc stop postgres
if dc run --rm backup once; then echo "backup succeeded with postgres down"; exit 1; fi
after="$(dc run --rm --entrypoint sh backup -c 'rclone cat "$RCLONE_REMOTE/last-success"')"
test "$before" = "$after"

echo "backup roundtrip OK"
```

`docker-compose.backup-test.yml`:

```yaml
# Isolated harness for tests/ops/backup_roundtrip.sh. The "remote" is a local directory
# (rclone accepts plain paths), so the same scripts run without any Google account.
services:
  postgres:
    image: postgres:16-alpine
    environment: {POSTGRES_USER: postgres, POSTGRES_PASSWORD: postgres, POSTGRES_DB: app}
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 2s, retries: 20}
  minio:
    image: quay.io/minio/minio:latest
    command: ["server", "/data"]
    environment: {MINIO_ROOT_USER: minioadmin, MINIO_ROOT_PASSWORD: minioadmin}
    healthcheck: {test: ["CMD", "mc", "ready", "local"], interval: 2s, retries: 20}
  minio-init:
    image: quay.io/minio/mc:latest
    profiles: ["manual"]
    environment: {MINIO_ROOT_USER: minioadmin, MINIO_ROOT_PASSWORD: minioadmin, S3_BUCKET_NAME: submissions-checker}
    entrypoint: ["/bin/sh", "/init.sh"]
    volumes: ["./docker/minio/init.sh:/init.sh:ro"]
  backup:
    build: {context: ., dockerfile: docker/backup/Dockerfile}
    profiles: ["manual"]
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: app
      S3_BUCKET_NAME: submissions-checker
      RCLONE_REMOTE: /remote/subchk
      RCLONE_CONFIG_MINIO_TYPE: s3
      RCLONE_CONFIG_MINIO_PROVIDER: Minio
      RCLONE_CONFIG_MINIO_ENDPOINT: http://minio:9000
      RCLONE_CONFIG_MINIO_ACCESS_KEY_ID: minioadmin
      RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY: minioadmin
    volumes: ["remote:/remote"]
volumes:
  remote:
```

- [ ] **Step 2: Run** `bash tests/ops/backup_roundtrip.sh` — FAILS (old image has no rclone / no `restore`).

- [ ] **Step 3: Implement.** `docker/backup/Dockerfile`:

```dockerfile
# Backup sidecar: pg_dump for the database, rclone for the object store and the off-host
# copy (Google Drive in production; any rclone remote or a plain path works).
FROM alpine:3.20

# postgresql-client must match the server's major version (16) — a newer pg_dump can
# read an older server, but an older pg_dump refuses a newer one.
RUN apk add --no-cache postgresql16-client rclone tzdata

COPY docker/backup/backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["schedule"]
```

`docker/backup/backup.sh`:

```sh
#!/bin/sh
# Backups of the two stores that hold data we cannot rebuild — PostgreSQL and the MinIO
# bucket — to an rclone remote ($RCLONE_REMOTE, e.g. gdrive:subchk). Nothing is kept on
# the local disk: a backup next to the database does not survive losing that disk.
#
#   backup.sh schedule        preflight, then run `once` on $BACKUP_CRON (the default)
#   backup.sh once            one backup now
#   backup.sh status          last successful backup and its age; non-zero if too old
#   backup.sh list            available database dumps, oldest first
#   backup.sh restore STAMP   restore DB + bucket from STAMP or `latest` (RESTORE_CONFIRM=yes)
#
# Remote layout:
#   postgres/<stamp>.dump      pg_dump --format=custom
#   minio/current/             mirror of the bucket
#   minio/deleted/<stamp>/     objects that run removed or overwrote in the mirror
#   pre-restore/<stamp>.dump   the database as it was just before a restore
#   last-success               stamp of the last fully successful run
set -eu

REMOTE="${RCLONE_REMOTE:-}"
BUCKET="minio:${S3_BUCKET_NAME:-submissions-checker}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-12}"
LOCK=/tmp/backup.lock
ENV_FILE=/tmp/backup.env
# Keep rclone's memory inside the container limit on a 2GB host.
RCLONE_FLAGS="--transfers 2 --checkers 4 --buffer-size 8M"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { log "ERROR: $*"; exit 1; }
stamp_now() { date -u +%Y%m%dT%H%M%SZ; }
pg() { PGPASSWORD="${POSTGRES_PASSWORD}" "$@" --host=postgres --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}"; }

# 20260928T060000Z -> epoch seconds (busybox date understands "YYYY-MM-DD hh:mm:ss").
stamp_epoch() {
	s="$1"
	date -u -d "$(echo "$s" | sed -E 's/^(....)(..)(..)T(..)(..)(..)Z$/\1-\2-\3 \4:\5:\6/')" +%s
}

preflight() {
	[ -n "${REMOTE}" ] || die "RCLONE_REMOTE is not set. Backups are off-host only; see docs/deployment.md#backups-and-restore"
	rclone mkdir "${REMOTE}" || die "cannot reach ${REMOTE} — check rclone.conf (RCLONE_CONFIG=${RCLONE_CONFIG:-default})"
	rclone lsf --max-depth 1 "${BUCKET}" >/dev/null || die "cannot read ${BUCKET} — check MinIO credentials"
	pg pg_isready >/dev/null || die "postgres is not ready"
}

# Delete dated entries (files "<stamp>.dump" or dirs "<stamp>/") older than the retention.
# Decided by the stamp in the NAME: rclone --backup-dir keeps an object's original mtime,
# so an mtime rule would delete a just-removed old object on the very next run.
prune_dir() {
	dir="$1"
	cutoff=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))
	rclone lsf "${dir}" 2>/dev/null | while IFS= read -r entry; do
		s="${entry%%.dump}"; s="${s%/}"
		case "$s" in [0-9]*T*Z) ;; *) continue ;; esac
		if [ "$(stamp_epoch "$s")" -lt "${cutoff}" ]; then
			case "$entry" in
				*/) rclone purge "${dir}/${s}" ;;
				*) rclone deletefile "${dir}/${entry}" ;;
			esac
			log "pruned ${dir}/${entry}"
		fi
	done
}

run_once() {
	preflight
	stamp="$(stamp_now)"
	dump="/tmp/${stamp}.dump"
	log "backup ${stamp} starting"

	# Database first, objects second: every row in the dump then has its object in the
	# mirror. The reverse order could capture rows whose objects were uploaded later.
	pg pg_dump --format=custom --file="${dump}" || { rm -f "${dump}"; die "pg_dump failed"; }
	rclone copyto ${RCLONE_FLAGS} "${dump}" "${REMOTE}/postgres/${stamp}.dump" || { rm -f "${dump}"; die "dump upload failed"; }
	rm -f "${dump}"
	log "database: postgres/${stamp}.dump"

	rclone sync ${RCLONE_FLAGS} "${BUCKET}" "${REMOTE}/minio/current" \
		--backup-dir "${REMOTE}/minio/deleted/${stamp}" || die "bucket sync failed"
	log "objects: minio/current (changes kept in minio/deleted/${stamp})"

	# Only a fully successful run prunes: a failing backup must never also delete the
	# last good one.
	prune_dir "${REMOTE}/postgres"
	prune_dir "${REMOTE}/minio/deleted"
	prune_dir "${REMOTE}/pre-restore"
	echo "${stamp}" | rclone rcat "${REMOTE}/last-success"
	log "backup ${stamp} complete"
}

locked() {
	exec 9>"${LOCK}"
	flock -n 9 || die "another backup or restore is running"
	"$@"
}

status() {
	last="$(rclone cat "${REMOTE}/last-success" 2>/dev/null)" || { echo "no successful backup yet"; exit 1; }
	age_h=$(( ($(date -u +%s) - $(stamp_epoch "${last}")) / 3600 ))
	echo "last successful backup: ${last} (${age_h}h ago)"
	[ "${age_h}" -le "${MAX_AGE_HOURS}" ] || { echo "OLDER THAN ${MAX_AGE_HOURS}h"; exit 1; }
}

list() { rclone lsf "${REMOTE}/postgres" | sed 's/\.dump$//' | sort; }

restore() {
	[ "${RESTORE_CONFIRM:-}" = "yes" ] || die "restore overwrites the database and bucket; set RESTORE_CONFIRM=yes"
	preflight
	want="${1:-}"
	[ -n "${want}" ] || die "usage: backup.sh restore <stamp|latest>"
	[ "${want}" = "latest" ] && want="$(list | tail -n 1)"
	[ -n "${want}" ] || die "no dumps found in ${REMOTE}/postgres"
	rclone lsf "${REMOTE}/postgres/${want}.dump" | grep -q . || die "no dump ${want}"

	safety="$(stamp_now)"
	pg pg_dump --format=custom --file="/tmp/pre-${safety}.dump" || die "safety dump failed; nothing changed"
	rclone copyto "/tmp/pre-${safety}.dump" "${REMOTE}/pre-restore/${safety}.dump" || die "safety dump upload failed; nothing changed"
	rm -f "/tmp/pre-${safety}.dump"
	log "current database saved to pre-restore/${safety}.dump"

	rclone copyto "${REMOTE}/postgres/${want}.dump" "/tmp/${want}.dump"
	# One transaction: a failed restore leaves the database exactly as it was.
	pg pg_restore --clean --if-exists --no-owner --single-transaction "/tmp/${want}.dump" \
		|| die "pg_restore failed; database unchanged (transaction rolled back)"
	rm -f "/tmp/${want}.dump"
	log "database restored from postgres/${want}.dump"

	rclone mkdir "${BUCKET}"
	rclone sync ${RCLONE_FLAGS} "${REMOTE}/minio/current" "${BUCKET}" || die "bucket restore failed (database already restored)"
	log "bucket restored from minio/current"
}

schedule() {
	preflight
	cron="${BACKUP_CRON:-0 */6 * * *}"
	# busybox crond does not pass the container environment to jobs.
	export -p > "${ENV_FILE}"
	echo "${cron} . ${ENV_FILE}; /usr/local/bin/backup.sh once > /proc/1/fd/1 2>&1" > /etc/crontabs/root
	log "scheduled '${cron}' (TZ=${TZ:-UTC}) to ${REMOTE}"
	exec crond -f -l 8
}

cmd="${1:-schedule}"
[ $# -gt 0 ] && shift
case "${cmd}" in
	schedule) schedule ;;
	once) locked run_once ;;
	status) status ;;
	list) list ;;
	restore) locked restore "$@" ;;
	*) die "unknown command: ${cmd} (schedule|once|status|list|restore)" ;;
esac
```
Busybox `flock` is available in alpine; confirm with `docker run --rm alpine:3.20 flock --help`. If it is missing, add `util-linux-misc` to `apk add`.

Prod compose `backup` service (replace the old one):

```yaml
  # Opt-in: COMPOSE_PROFILES=backup in .env (comma-join with observability). Backups go
  # only to $RCLONE_REMOTE (Google Drive) — a copy on this disk would die with it. Env uses
  # `:-` not `:?`: compose interpolates profiled services even when they are off.
  # Runbook: docs/deployment.md#backups-and-restore; scripts/ops/*-prod-*.sh wrap it.
  backup:
    build:
      context: .
      dockerfile: docker/backup/Dockerfile
    image: submissions-checker-backup:local
    pull_policy: build
    profiles: ["backup"]
    restart: unless-stopped
    environment:
      TZ: ${BACKUP_TZ:-Europe/Kyiv}
      BACKUP_CRON: ${BACKUP_CRON:-0 */6 * * *}
      BACKUP_RETENTION_DAYS: ${BACKUP_RETENTION_DAYS:-30}
      RCLONE_REMOTE: ${RCLONE_REMOTE:-}
      RCLONE_CONFIG: /config/rclone/rclone.conf
      POSTGRES_USER: ${POSTGRES_USER:-}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-}
      POSTGRES_DB: ${POSTGRES_DB:-submissions_checker}
      S3_BUCKET_NAME: ${S3_BUCKET_NAME:-submissions-checker}
      RCLONE_CONFIG_MINIO_TYPE: s3
      RCLONE_CONFIG_MINIO_PROVIDER: Minio
      RCLONE_CONFIG_MINIO_ENDPOINT: http://minio:9000
      RCLONE_CONFIG_MINIO_ACCESS_KEY_ID: ${MINIO_ROOT_USER:-}
      RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD:-}
    volumes:
      # A directory, not the file, and writable: rclone rewrites rclone.conf (atomically,
      # via a temp file) whenever it refreshes the Drive token.
      - ${RCLONE_CONFIG_DIR:-./docker/backup/rclone}:/config/rclone
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_healthy
    networks: [data]
    mem_limit: 128m
    logging: *default-logging
    labels:
      com.centurylinklabs.watchtower.enable: "false"
```
`data` network: it must reach Drive — `data` is not `internal`, so outbound works (the header comment on networks says so; keep it true).
Header memory budget: `backup 128M (rclone; only when the backup profile is on)`, recompute `committed ~1716M, leaving ~320M`.
`.gitignore`: `docker/backup/rclone/`.

- [ ] **Step 4: Run** `bash tests/ops/backup_roundtrip.sh` → ends with `backup roundtrip OK`. Also `docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable docker/backup/backup.sh tests/ops/backup_roundtrip.sh` → no findings (fix or annotate with `# shellcheck disable=SC2086` on the intentional unquoted `${RCLONE_FLAGS}` lines). `docker compose -f docker-compose.prod.yml --env-file <scratch env> config -q` with and without `COMPOSE_PROFILES=backup`.
- [ ] **Step 5: Commit** — `Make backups opt-in, off-host and every six hours`.

---

### Task 13: Host-side ops scripts

**Files:**
- Create: `scripts/ops/_prod.sh`, `scripts/ops/connect-to-prod-db.sh`, `scripts/ops/run-prod-backup.sh`, `scripts/ops/restore-prod-from-backup.sh`, `scripts/ops/prod.env.example`
- Test: `tests/ops/test_ops_scripts.sh`

- [ ] **Step 1: Test first** — `tests/ops/test_ops_scripts.sh` (no network; a fake `ssh` on PATH records what would run):

```bash
#!/usr/bin/env bash
# Offline checks for scripts/ops: help text, config errors, and the exact remote commands.
set -euo pipefail
cd "$(dirname "$0")/../.."
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/ssh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_LOG"
EOF
chmod +x "$tmp/bin/ssh"
export PATH="$tmp/bin:$PATH" SSH_LOG="$tmp/ssh.log" PROD_ENV_FILE="$tmp/none"

declare -A valid_args=(
  [connect-to-prod-db]='-c select-1'
  [run-prod-backup]='--status'
  [restore-prod-from-backup]='latest --yes'
)
for s in "${!valid_args[@]}"; do
  scripts/ops/$s.sh --help | grep -q "Usage:" || { echo "$s: no usage"; exit 1; }
  # shellcheck disable=SC2086
  if PROD_SSH= PROD_DIR= scripts/ops/$s.sh ${valid_args[$s]} 2>"$tmp/err"; then echo "$s ran without PROD_SSH"; exit 1; fi
  grep -q "PROD_SSH" "$tmp/err" || { echo "$s: unclear error"; exit 1; }
done
test ! -s "$SSH_LOG" || { echo "a script reached ssh without config"; exit 1; }

export PROD_SSH=user@host PROD_DIR=/srv/app
scripts/ops/run-prod-backup.sh --status
grep -q "exec -T backup backup.sh status" "$SSH_LOG"

scripts/ops/connect-to-prod-db.sh -c "select 1"
grep -q "default_transaction_read_only=on" "$SSH_LOG"

: > "$SSH_LOG"
if echo "no" | scripts/ops/restore-prod-from-backup.sh latest; then echo "restore ran without confirmation"; exit 1; fi
test ! -s "$SSH_LOG" || { echo "restore touched the host without confirmation"; exit 1; }

scripts/ops/restore-prod-from-backup.sh latest --yes
grep -q "stop app" "$SSH_LOG"
grep -q "RESTORE_CONFIRM=yes backup restore latest" "$SSH_LOG"
grep -q "up -d app" "$SSH_LOG"
echo "ops scripts OK"
```

- [ ] **Step 2: Run** — fails (scripts missing).

- [ ] **Step 3: Implement.**

`scripts/ops/prod.env.example`:
```bash
# Copy to ssh/prod.env (gitignored) and fill in. Read by scripts/ops/*.sh.
PROD_SSH=root@203.0.113.10          # ssh target of the production host
PROD_DIR=/opt/submissions-checker   # directory holding docker-compose.prod.yml and .env
```

`scripts/ops/_prod.sh` (sourced, not executable):
```bash
# Shared helpers for scripts/ops. Sourced; do not run directly.
# Production address comes from the environment or ssh/prod.env — never from git.
PROD_ENV_FILE="${PROD_ENV_FILE:-$(dirname "${BASH_SOURCE[0]}")/../../ssh/prod.env}"
# shellcheck disable=SC1090
[[ -f "$PROD_ENV_FILE" ]] && source "$PROD_ENV_FILE"

require_prod() {
  if [[ -z "${PROD_SSH:-}" || -z "${PROD_DIR:-}" ]]; then
    echo "PROD_SSH and PROD_DIR must be set (env or ssh/prod.env; see scripts/ops/prod.env.example)" >&2
    exit 2
  fi
}

# Run a compose command on the production host: prod_compose exec -T backup backup.sh status
prod_compose() {
  ssh "${SSH_TTY_FLAG[@]}" "$PROD_SSH" \
    "cd $(printf %q "$PROD_DIR") && docker compose -f docker-compose.prod.yml --env-file .env $(printf '%q ' "$@")"
}
SSH_TTY_FLAG=()
```
(`printf %q` quoting survives the remote shell. Scripts needing a TTY set `SSH_TTY_FLAG=(-t)`.)

`scripts/ops/connect-to-prod-db.sh`:
```bash
#!/usr/bin/env bash
# Open psql on the production database over SSH — read-only unless --write.
set -euo pipefail
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: connect-to-prod-db.sh [--write] [--tunnel [LOCAL_PORT]] [-c SQL]

  (no args)       interactive psql inside the prod postgres container, READ-ONLY session
  -c SQL          run one statement and print the result (read-only unless --write)
  --write         allow writes (think twice; there is no undo besides a restore)
  --tunnel [PORT] forward localhost:PORT (default 15432) to prod postgres for a GUI client;
                  credentials are POSTGRES_USER/POSTGRES_PASSWORD from the prod .env

Needs PROD_SSH and PROD_DIR (env or ssh/prod.env).
EOF
}

mode=psql write=0 sql="" port=15432
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --write) write=1 ;;
    --tunnel) mode=tunnel; [[ "${2:-}" =~ ^[0-9]+$ ]] && { port="$2"; shift; } ;;
    -c) sql="${2:?-c needs SQL}"; shift ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
require_prod

if [[ "$mode" == tunnel ]]; then
  echo "Forwarding localhost:${port} -> prod postgres (Ctrl-C to close)"
  exec ssh -N -L "${port}:127.0.0.1:${POSTGRES_HOST_PORT:-5432}" "$PROD_SSH"
fi

opts="-c default_transaction_read_only=on"
[[ $write -eq 1 ]] && opts=""
inner='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
if [[ -n "$sql" ]]; then
  prod_compose exec -T -e "PGOPTIONS=${opts}" postgres sh -c "${inner} -v ON_ERROR_STOP=1 -c $(printf %q "$sql")"
else
  SSH_TTY_FLAG=(-t)
  prod_compose exec -e "PGOPTIONS=${opts}" postgres sh -c "${inner}"
fi
```

`scripts/ops/run-prod-backup.sh`:
```bash
#!/usr/bin/env bash
# Trigger or inspect production backups (the `backup` service must be enabled on prod).
set -euo pipefail
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: run-prod-backup.sh [--now | --status | --list | --logs]

  --now     take a backup right now (e.g. before a risky deploy or migration)
  --status  when the last successful backup ran; exits non-zero if older than 12h (default)
  --list    stamps of available database dumps, oldest first (use one with restore)
  --logs    follow the backup container's log

Backups run by themselves every 6 hours; this is for the moments in between.
Needs PROD_SSH and PROD_DIR (env or ssh/prod.env).
EOF
}

action="${1:---status}"
case "$action" in
  -h|--help) usage; exit 0 ;;
  --now) cmd=(exec -T backup backup.sh once) ;;
  --status) cmd=(exec -T backup backup.sh status) ;;
  --list) cmd=(exec -T backup backup.sh list) ;;
  --logs) cmd=(--profile backup logs -f --tail 100 backup) ;;
  *) echo "unknown argument: $action" >&2; usage >&2; exit 2 ;;
esac
require_prod
prod_compose "${cmd[@]}" || {
  echo "Failed. If the service is not running, enable it: COMPOSE_PROFILES=backup in the prod .env (docs/deployment.md#backups-and-restore)." >&2
  exit 1
}
```

`scripts/ops/restore-prod-from-backup.sh`:
```bash
#!/usr/bin/env bash
# DESTRUCTIVE: replace the production database and bucket with a backup.
set -euo pipefail
source "$(dirname "$0")/_prod.sh"

usage() {
  cat <<'EOF'
Usage: restore-prod-from-backup.sh <STAMP|latest> [--yes]

Replaces the production database AND the MinIO bucket with backup STAMP
(see: run-prod-backup.sh --list). Steps: stop app -> save current DB to
pre-restore/<now>.dump on the remote -> pg_restore in one transaction ->
sync bucket -> start app. The app is started again even if the restore fails.

Works on a fresh host too (the backup profile need not be enabled).
--yes skips the typed confirmation. Needs PROD_SSH and PROD_DIR.
EOF
}

stamp="" yes=0
for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
    --yes) yes=1 ;;
    -*) echo "unknown argument: $a" >&2; usage >&2; exit 2 ;;
    *) stamp="$a" ;;
  esac
done
[[ -n "$stamp" ]] || { usage >&2; exit 2; }
require_prod

if [[ $yes -ne 1 ]]; then
  echo "This REPLACES the database and bucket on ${PROD_SSH} with backup '${stamp}'."
  read -r -p "Type 'restore' to continue: " answer
  [[ "$answer" == "restore" ]] || { echo "aborted"; exit 1; }
fi

prod_compose stop app
trap 'prod_compose up -d app' EXIT
prod_compose --profile backup run --rm -e RESTORE_CONFIRM=yes backup restore "$stamp"
echo "Restore of ${stamp} finished; starting app."
```

`chmod +x scripts/ops/*.sh tests/ops/*.sh` (not `_prod.sh`).

- [ ] **Step 4: Run** `bash tests/ops/test_ops_scripts.sh` → `ops scripts OK`; shellcheck all of `scripts/ops/*.sh tests/ops/*.sh` via the docker image.
- [ ] **Step 5: Commit** — `Add host scripts to reach the prod DB and run or restore backups`.

---

### Task 14: Makefile cleanup and command docs

**Files:**
- Rewrite: `Makefile`
- Modify: `dev_setup.sh` (the `uv venv` / `uv pip install` block), `README.md` (make up line, localstack mentions)
- Create: `docs/commands.md`

- [ ] **Step 1: Rewrite `Makefile`**:

```makefile
# Thin aliases. Logic lives in scripts/ (documented in docs/commands.md); run `make help`.
UV := uv run --frozen --extra dev
E2E_ENV := E2E_APP_URL=http://localhost:8001 \
	E2E_DB_URL=postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e
SHELLCHECK := docker run --rm -v "$(CURDIR):/mnt" -w /mnt koalaman/shellcheck:stable

.PHONY: help install setup vendor-assets up down logs logs-app db-shell health api-docs \
	test test-unit test-integration test-functional test-ops test-backup \
	lint lint-fix format format-check type-check shellcheck quality clean \
	e2e e2e-up e2e-down e2e-logs \
	observability-up observability-down alloy-logs dashboards-json dashboards alerting \
	prod-db prod-backup prod-backup-status

help: ## List targets (details: docs/commands.md)
	@awk 'BEGIN {FS = ":.*?## "} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Dev
install: ## Install locked Python deps into .venv (after pulling a lockfile change)
	uv sync --frozen --extra dev

setup: vendor-assets ## First-time setup: deps, .env from example, git hooks
	./dev_setup.sh

vendor-assets: ## Fetch proctoring models into static/vendor/ (idempotent; up runs it)
	python3 scripts/fetch_vendor_assets.py

up: vendor-assets ## Start dev stack: postgres, minio, app on :8000 (hot reload)
	docker compose up -d

down: ## Stop dev stack (data volumes kept)
	docker compose down

logs: ## Follow all dev logs
	docker compose logs -f

logs-app: ## Follow app log only
	docker compose logs -f app

db-shell: ## psql into the dev database
	docker compose exec postgres psql -U postgres -d submissions_checker

health: ## Probe /health and /health/ready of the dev app
	@curl -fsS http://localhost:8000/health && echo
	@curl -fsS http://localhost:8000/health/ready && echo

api-docs: ## Open Swagger UI of the dev app
	@xdg-open http://localhost:8000/docs 2>/dev/null || open http://localhost:8000/docs 2>/dev/null || echo http://localhost:8000/docs

##@ Test
test: ## Whole Python suite with coverage (~6 min, needs Docker)
	$(UV) pytest --cov=submissions_checker --cov-report=term-missing --cov-report=html

test-unit: ## Unit tests only (seconds, no Docker)
	$(UV) pytest tests/unit -q

test-integration: ## Integration tests (testcontainers Postgres)
	$(UV) pytest tests/integration -q

test-functional: ## Functional API tests (real app over ASGI)
	$(UV) pytest tests/functional -q

test-ops: ## Offline checks of scripts/ops (no network)
	bash tests/ops/test_ops_scripts.sh

test-backup: ## Backup -> destroy -> restore roundtrip in an isolated compose project (~2 min)
	bash tests/ops/backup_roundtrip.sh

##@ Quality
lint: ## Ruff lint
	$(UV) ruff check src tests

lint-fix: ## Ruff lint with autofix
	$(UV) ruff check --fix src tests

format: ## Ruff format (writes)
	$(UV) ruff format src tests

format-check: ## Ruff format check (CI)
	$(UV) ruff format --check src tests

type-check: ## mypy on src
	$(UV) mypy src

shellcheck: ## shellcheck all shell scripts (via Docker)
	$(SHELLCHECK) docker/backup/backup.sh docker/minio/init.sh scripts/ops/*.sh tests/ops/*.sh dev_setup.sh

quality: lint format-check type-check shellcheck ## Everything CI checks except tests

clean: ## Remove caches and coverage output
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name htmlcov \) -prune -exec rm -rf {} +
	rm -f .coverage coverage.xml

##@ E2E
e2e: vendor-assets ## Browser tests. TAGS=@tag SCENARIO="name" FILE=path HEADED=1
	docker compose -f docker-compose.e2e.yml up -d --build --wait
	$(E2E_ENV) uv run --frozen --extra e2e pytest -c pytest-e2e.ini tests/e2e/ -v \
		$(if $(HEADED),--headed,) $(if $(TAGS),-m "$(TAGS)",) \
		$(if $(SCENARIO),-k "$(SCENARIO)",) $(if $(FILE),$(FILE),) \
		|| (docker compose -f docker-compose.e2e.yml down; exit 1)
	docker compose -f docker-compose.e2e.yml down

e2e-up: ## Start the e2e stack only (to debug against :8001)
	docker compose -f docker-compose.e2e.yml up -d --build --wait

e2e-down: ## Stop the e2e stack
	docker compose -f docker-compose.e2e.yml down

e2e-logs: ## Follow e2e app log
	docker compose -f docker-compose.e2e.yml logs -f app-e2e

##@ Observability (docs/observability.md)
observability-up: ## Local Prometheus+Grafana+Alloy; Grafana at :3000
	docker compose --profile observability up -d prometheus grafana alloy

observability-down: ## Stop the local observability harness
	docker compose --profile observability rm -sf prometheus grafana alloy

alloy-logs: ## Follow Alloy (rejected pushes show here)
	docker compose --profile observability logs -f alloy

dashboards-json: ## Regenerate dashboard/alert JSON (never hand-edit them)
	python3 observability/grafana/build_dashboards.py
	python3 observability/grafana/build_alerting.py

dashboards: ## Push dashboards to Grafana Cloud (GRAFANA_* in .env)
	@set -a; . ./.env; set +a; python3 observability/grafana/push.py dashboards

alerting: ## Push contact point + alert rules to Grafana Cloud
	@set -a; . ./.env; set +a; python3 observability/grafana/push.py alerting

##@ Prod ops (need ssh/prod.env; restore has no target on purpose)
prod-db: ## Read-only psql on prod (scripts/ops/connect-to-prod-db.sh)
	scripts/ops/connect-to-prod-db.sh

prod-backup: ## Take a prod backup now (before risky deploys)
	scripts/ops/run-prod-backup.sh --now

prod-backup-status: ## Age of the last good prod backup
	scripts/ops/run-prod-backup.sh --status
```
(Recipe lines must be TAB-indented.) Verify there was a git-hooks step in `dev_setup.sh` before claiming "git hooks" in the `setup` help; adjust the text to what the script actually does.

- [ ] **Step 2: `dev_setup.sh`** — replace the `uv venv` block and `uv pip install -e ".[dev]"` with `uv sync --frozen --extra dev`.

- [ ] **Step 3: `docs/commands.md`** — one section per `##@` group; for every target and every `scripts/ops/*.sh`: **what it does**, **when to use it**, **prerequisites**, **destructive?**. Include a short "Which do I need?" list at the top: daily dev (`up`, `logs-app`, `test-unit`, `lint`), before pushing (`quality`, `test`), prod (`prod-backup-status`, `prod-db`), rarely (`e2e`, `test-backup`, observability). Document the restore script (no make target — why). Document removed targets and their replacements (`dev`→`up`, `venv`/`activate`→`install` + `uv run`, `build`→`docker compose build`, `test-watch`/`shell` gone).

- [ ] **Step 4: README** — fix the `make up` comment (postgres + minio + app; observability is `make observability-up`), replace localstack mentions, link `docs/commands.md`.

- [ ] **Step 5: Verify** — `make help` renders groups; `make lint type-check format-check test-unit test-ops shellcheck` all pass.
- [ ] **Step 6: Commit** — `Make Makefile targets reliable and document when to use them` (body: several targets silently depended on an activated venv, bypassed uv.lock, or did nothing).

---

### Task 15: Skills and deployment docs

**Files:**
- Create: `.claude/skills/connect-to-prod-db/SKILL.md`, `.claude/skills/run-prod-backup/SKILL.md`, `.claude/skills/restore-prod-from-backup/SKILL.md`
- Modify: `docs/deployment.md` (§ Backups and restore, bootstrap `BACKUP_DIR` mentions ~179-210, rebuild note ~494-509, memory table ~533, new § Storage migration), `.env.example`/prod env docs if they list `BACKUP_DIR`/`BACKUP_INTERVAL_SECONDS`/`HOST_PLUGINS_DIR`
- Modify (local-only, gitignored): `.claude/CLAUDE.md` — Commands + docs map pointers

- [ ] **Step 1: Skills.**

`.claude/skills/connect-to-prod-db/SKILL.md`:
```markdown
---
name: connect-to-prod-db
description: Query the production PostgreSQL of submissions-checker (read-only by default) — use when the user asks about live/prod data, counts, a specific student/submission in production, or to inspect prod schema state.
---

# Connect to the production database

Run `scripts/ops/connect-to-prod-db.sh -c "<SQL>"` — one statement, output printed.
The session is READ-ONLY (`default_transaction_read_only=on`).

- Needs `PROD_SSH` and `PROD_DIR` (env or `ssh/prod.env`, template `scripts/ops/prod.env.example`). If missing, ask the user for them; never guess a host.
- Prefer narrow queries with `LIMIT`; the host has 2GB RAM and serves students.
- Treat results as personal data: summarise, do not paste whole tables of students.
- `--write` exists. Use it only when the user explicitly asked for that specific change in this conversation, and suggest `scripts/ops/run-prod-backup.sh --now` first.
- `--tunnel` is for the user's GUI client; do not start it yourself (it blocks).
```

`.claude/skills/run-prod-backup/SKILL.md`:
```markdown
---
name: run-prod-backup
description: Take, check or list submissions-checker production backups (Postgres + MinIO to Google Drive) — use before risky deploys/migrations, when asked whether backups work, or to find a restore point.
---

# Production backups

Backups run on prod every 6 hours when `COMPOSE_PROFILES` includes `backup`.

| Need | Command |
|---|---|
| Is the last backup recent? | `scripts/ops/run-prod-backup.sh --status` (non-zero exit if > 12h) |
| Backup right now | `scripts/ops/run-prod-backup.sh --now` |
| Restore points | `scripts/ops/run-prod-backup.sh --list` |
| Container log | `scripts/ops/run-prod-backup.sh --logs` (blocks; for the user) |

- Needs `PROD_SSH`/`PROD_DIR` (see `connect-to-prod-db`).
- "service not running" → backups are not enabled on prod; point the user to `docs/deployment.md#backups-and-restore`. Do not edit the prod `.env` yourself.
- Restoring is a different, user-only skill (`/restore-prod-from-backup`). Never restore from here.
```

`.claude/skills/restore-prod-from-backup/SKILL.md`:
```markdown
---
name: restore-prod-from-backup
description: DESTRUCTIVE restore of submissions-checker production (Postgres + MinIO) from a Google Drive backup. User-invoked only.
disable-model-invocation: true
---

# Restore production from a backup

Replaces the prod database AND bucket. Only when the user invoked this skill.

1. `scripts/ops/run-prod-backup.sh --list` — show stamps; ask which one (default `latest`). Say plainly that everything after that stamp is lost from the live system (it stays in `pre-restore/` on Drive).
2. Get an explicit "yes, restore <stamp>" from the user in this conversation.
3. `scripts/ops/restore-prod-from-backup.sh <stamp> --yes`
4. Verify: `curl -fsS https://<domain>/health/ready` (ask for the domain if unknown) and `scripts/ops/connect-to-prod-db.sh -c "select max(created_at) from submissions"` — report both.

If the restore fails, the database is unchanged (single transaction) and the app is restarted by the script; report the error output verbatim.
```

- [ ] **Step 2: `docs/deployment.md`.** Replace § Backups and restore with: what is backed up and where (remote layout), enabling (rclone config on a laptop: `rclone config` → new remote `gdrive`, type `drive`, scope `drive.file`, auto config yes → copy `~/.config/rclone/rclone.conf` to `<PROD_DIR>/docker/backup/rclone/rclone.conf`; `.env`: `COMPOSE_PROFILES=backup` (or `observability,backup`), `RCLONE_REMOTE=gdrive:subchk`; `docker compose -f docker-compose.prod.yml --env-file .env build backup && … up -d --force-recreate backup`; first run `scripts/ops/run-prod-backup.sh --now` then `--status`), schedule/retention knobs, restore (the script; fresh-host variant: install compose stack, put `.env` + `rclone.conf`, `up -d postgres minio minio-init`, run restore), privacy note (unencrypted; 2FA; do not share the folder; `crypt` remote switch), what a failing run does. Add § Storage migration (this release): deploy → `docker compose … run --rm app python -m submissions_checker.cli.migrate_uploads` → re-apply any listed subjects → when it reports `missing: 0` and no subjects, phase 2 may remove `uploads` + plugins mounts. Rollout of the new backup: the old container keeps running until `up -d --force-recreate backup` with the profile; delete old `BACKUP_DIR` contents only after the first Drive backup is confirmed. Remove `BACKUP_DIR`/`BACKUP_INTERVAL_SECONDS` everywhere (`grep -rn "BACKUP_DIR\|BACKUP_INTERVAL" --exclude-dir=.git .`). Update the memory table row to `backup | 128M | only with the backup profile`.

- [ ] **Step 3: `.claude/CLAUDE.md`** (local file): add under Commands `make help` / `docs/commands.md`, `make test-backup`, `scripts/ops/*` + the three skills; in the tree note `services/submission_files.py`, `services/plugin_cache.py`, `cli/migrate_uploads.py`; remove "localstack" from the `make up` line; docs map row for `docs/commands.md`.

- [ ] **Step 4: Verify** — `grep -rn "localstack\|BACKUP_DIR\|BACKUP_INTERVAL" --exclude-dir=.git --exclude-dir=node_modules .` returns only intentional historical mentions (specs/plans/changelog).
- [ ] **Step 5: Commit** — `Document backups, storage migration and prod ops skills`.

---

### Task 16: Full verification, merge, push

- [ ] **Step 1:** `uv run --frozen --extra dev pytest -q` — read the summary line; 0 failures.
- [ ] **Step 2:** `uv run --frozen ruff check src/ tests/ && uv run --frozen ruff format --check src/ tests/ && uv run --frozen mypy src/` — clean.
- [ ] **Step 3:** `make shellcheck test-ops test-backup` — `ops scripts OK`, `backup roundtrip OK`.
- [ ] **Step 4:** `make e2e` — all pass.
- [ ] **Step 5:** Non-breaking check against the prod compose with a copy of a prod-shaped `.env` lacking every new variable (no `COMPOSE_PROFILES`, no `RCLONE_*`, old `HOST_PLUGINS_DIR` still set): `docker compose -f docker-compose.prod.yml --env-file <that file> config -q` succeeds and `config --services` does not list `backup`.
- [ ] **Step 6:** Request a whole-branch review (superpowers:requesting-code-review); fix confirmed findings with tests.
- [ ] **Step 7:** `git switch main && git pull --ff-only && git merge --no-ff storage-to-minio-and-drive-backups` (message: why), re-run `uv run --frozen --extra dev pytest -q` on main, then `git push origin main`.
- [ ] **Step 8:** Tell the user the manual prod steps, in order: (1) watch Watchtower roll out the app; (2) run `migrate_uploads`, re-apply listed subjects; (3) create `rclone.conf`, set `COMPOSE_PROFILES`/`RCLONE_REMOTE`, build + recreate `backup`; (4) `run-prod-backup.sh --now` and `--status`; (5) check Drive; (6) only then clear the old `BACKUP_DIR`.
