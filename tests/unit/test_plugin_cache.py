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
    assert list(tmp_path.iterdir()) == []


def test_incomplete_tree_is_restored(tmp_path) -> None:
    """A host /tmp cleaner (e.g. systemd-tmpfiles) can remove individual unread files
    from an otherwise-fresh tree without touching its mtime. Reuse must notice and
    re-extract rather than handing the sandbox a tree that is missing files."""
    z = _zip({"a.txt": "1", "b/c.txt": "2"})
    out = materialize_plugin_tree(tmp_path, "demo", z)
    (out / "b/c.txt").unlink()
    restored = materialize_plugin_tree(tmp_path, "demo", z)
    assert restored == out
    assert (restored / "a.txt").read_text() == "1"
    assert (restored / "b/c.txt").read_text() == "2"


def test_target_pruned_between_check_and_utime_triggers_reextract(tmp_path, monkeypatch) -> None:
    """Another replica's idle sweep can remove the whole target dir in the gap between
    this call's completeness check and its os.utime() touch. That must be handled like
    any other missing/incomplete tree, not raise FileNotFoundError out of this call."""
    import shutil

    z = _zip({"a.txt": "1"})
    out = materialize_plugin_tree(tmp_path, "demo", z)

    real_utime = os.utime
    calls = {"n": 0}

    def flaky_utime(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            shutil.rmtree(path, ignore_errors=True)
            raise FileNotFoundError(path)
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(plugin_cache.os, "utime", flaky_utime)
    restored = materialize_plugin_tree(tmp_path, "demo", z)
    assert restored == out
    assert (restored / "a.txt").read_text() == "1"
    assert calls["n"] >= 2


def test_old_dir_pruned_like_tmp(tmp_path) -> None:
    """.old-* leftovers from a re-extraction race follow the same idle-prune rule as
    .tmp-* leftovers."""
    stale_old = tmp_path / ".old-deadbeef"
    stale_old.mkdir()
    past = time.time() - 2 * 86400
    os.utime(stale_old, (past, past))
    materialize_plugin_tree(tmp_path, "demo", _zip({"v": "1"}))
    assert not stale_old.exists()
