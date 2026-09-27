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
