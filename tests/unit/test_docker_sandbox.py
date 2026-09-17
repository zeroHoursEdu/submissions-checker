"""Unit tests for DockerSandbox.

asyncio.create_subprocess_exec is patched so no real `docker` process is spawned.
Tests assert the exact `docker run` argv (isolation flags, mounts, resource
limits, env), result parsing, the timeout-kill path, output-dir read-back, and
the FileNotFoundError -> RuntimeError translation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from submissions_checker.services.docker_sandbox import (
    DockerSandbox,
    SandboxResult,
)


def _fake_proc(stdout=b"", stderr=b"", returncode=0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = lambda: None
    return proc


def _patch_exec(proc):
    """Patch create_subprocess_exec; return the patcher + a recorder for argv.

    Records every call (not just the last) in `recorder["calls"]`, since a timed-out
    run issues a second `docker kill` subprocess in addition to the original `docker
    run` — `recorder["cmd"]` stays as a convenience alias for the most recent call.
    """
    recorder: dict = {"calls": []}

    async def fake_exec(*cmd, **kwargs):
        recorder["cmd"] = list(cmd)
        recorder["kwargs"] = kwargs
        recorder["calls"].append(list(cmd))
        return proc

    p = patch(
        "submissions_checker.services.docker_sandbox.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    )
    return p, recorder


async def test_run_builds_isolation_argv(tmp_path: Path) -> None:
    student = tmp_path / "submission"
    plugin = tmp_path / "plugin"
    student.mkdir()
    plugin.mkdir()

    proc = _fake_proc(stdout=b"out", stderr=b"err", returncode=0)
    patcher, rec = _patch_exec(proc)
    with patcher:
        result = await DockerSandbox().run(
            image="demo:local",
            tool="python",
            script_path="assignments/lab1/check.py",
            student_files_dir=student,
            plugin_dir=plugin,
            env={"FOO": "bar"},
            memory="128m",
            cpus=0.25,
            timeout=15,
        )

    cmd = rec["cmd"]
    # core isolation / safety flags
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert "--memory=128m" in cmd
    assert "--cpus=0.25" in cmd
    assert "--pids-limit=100" in cmd
    assert "--read-only" in cmd
    assert "--tmpfs" in cmd
    # no Linux capabilities, and none can be regained via setuid binaries in the image
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    # mounts: submission + plugin read-only, output writable
    assert f"{student}:/submission:ro" in cmd
    assert f"{plugin}:/plugin:ro" in cmd
    assert any(c.endswith(":/output:rw") for c in cmd)
    # env passed through
    assert "FOO=bar" in cmd
    # trailing command: image, tool, /plugin/<script>, /submission
    assert cmd[-4:] == ["demo:local", "python", "/plugin/assignments/lab1/check.py", "/submission"]

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "out"
    assert result.stderr == "err"


async def test_run_uses_default_limits(tmp_path: Path) -> None:
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()
    patcher, rec = _patch_exec(_fake_proc())
    with patcher:
        await DockerSandbox().run(
            image="i",
            tool="python",
            script_path="c.py",
            student_files_dir=student,
            plugin_dir=plugin,
        )
    assert "--memory=256m" in rec["cmd"]
    assert "--cpus=0.5" in rec["cmd"]


async def test_run_parses_output_files(tmp_path: Path) -> None:
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()

    # Write a result.json into whatever temp output dir the sandbox creates.
    real_tempdir = __import__("tempfile").TemporaryDirectory

    captured = {}

    class _TD:
        def __init__(self, *a, **k):
            self._td = real_tempdir(*a, **k)
            self.name = self._td.name
            captured["dir"] = Path(self.name)
            (Path(self.name) / "result.json").write_text('{"ok": true}', encoding="utf-8")

        def __enter__(self):
            return self.name

        def __exit__(self, *a):
            return self._td.__exit__(*a)

    proc = _fake_proc(returncode=0)
    patcher, _ = _patch_exec(proc)
    with (
        patcher,
        patch("submissions_checker.services.docker_sandbox.tempfile.TemporaryDirectory", _TD),
    ):
        result = await DockerSandbox().run(
            image="i",
            tool="python",
            script_path="c.py",
            student_files_dir=student,
            plugin_dir=plugin,
        )
    assert result.output_files["result.json"] == '{"ok": true}'


async def test_run_nonzero_exit_code(tmp_path: Path) -> None:
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()
    patcher, _ = _patch_exec(_fake_proc(returncode=2, stderr=b"boom"))
    with patcher:
        result = await DockerSandbox().run(
            image="i",
            tool="python",
            script_path="c.py",
            student_files_dir=student,
            plugin_dir=plugin,
        )
    assert result.exit_code == 2
    assert result.stderr == "boom"


async def test_run_timeout_kills_and_returns_sentinel(tmp_path: Path) -> None:
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()

    killed = {"called": False}
    proc = AsyncMock()
    # asyncio.wait_for is what raises TimeoutError; patch it to simulate timeout.
    # After the kill, run() awaits proc.communicate() once more to reap the proc.
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = -1

    def _kill():
        killed["called"] = True

    proc.kill = _kill

    patcher, rec = _patch_exec(proc)
    with (
        patcher,
        patch(
            "submissions_checker.services.docker_sandbox.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ),
    ):
        result = await DockerSandbox().run(
            image="i",
            tool="python",
            script_path="c.py",
            student_files_dir=student,
            plugin_dir=plugin,
            timeout=1,
        )
    assert killed["called"] is True
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()
    assert result.stdout == ""

    # A timeout must also kill the actual container (docker_sandbox.py:84-96 bug #7),
    # not just the local `docker run` CLI wrapper — `proc.kill()` alone leaves the
    # container running under the daemon since --rm only removes it once it stops.
    run_cmd, kill_cmd = rec["calls"]
    assert run_cmd[:2] == ["docker", "run"]
    name_idx = run_cmd.index("--name")
    container_name = run_cmd[name_idx + 1]
    assert container_name.startswith("submission-check-")
    assert kill_cmd == ["docker", "kill", container_name]


async def test_run_timeout_kill_failure_is_swallowed(tmp_path: Path) -> None:
    """A `docker kill` racing against a container that already exited on its own
    (or any kill failure) must not surface as an error — the timeout result is
    still returned normally, not an unhandled exception."""
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()

    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = -1
    proc.kill = lambda: None

    call_count = {"n": 0}

    async def fake_exec(*cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return proc  # the original `docker run`
        raise OSError("container already gone")  # the `docker kill` call

    with (
        patch(
            "submissions_checker.services.docker_sandbox.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch(
            "submissions_checker.services.docker_sandbox.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ),
    ):
        result = await DockerSandbox().run(
            image="i",
            tool="python",
            script_path="c.py",
            student_files_dir=student,
            plugin_dir=plugin,
            timeout=1,
        )

    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()


async def test_run_missing_docker_binary_raises_runtime_error(tmp_path: Path) -> None:
    student = tmp_path / "s"
    plugin = tmp_path / "p"
    student.mkdir()
    plugin.mkdir()
    with patch(
        "submissions_checker.services.docker_sandbox.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(RuntimeError, match="docker command not found"):
            await DockerSandbox().run(
                image="i",
                tool="python",
                script_path="c.py",
                student_files_dir=student,
                plugin_dir=plugin,
            )


def test_read_output_dir_skips_large_and_missing(tmp_path: Path) -> None:
    sandbox = DockerSandbox()
    # missing dir -> empty
    assert sandbox._read_output_dir(tmp_path / "nope") == {}

    out = tmp_path / "out"
    out.mkdir()
    (out / "small.txt").write_text("hi", encoding="utf-8")
    big = out / "big.bin"
    big.write_bytes(b"x" * 1_048_577)  # over 1 MB -> skipped
    (out / "sub").mkdir()  # directory -> skipped

    files = sandbox._read_output_dir(out)
    assert files == {"small.txt": "hi"}


def test_read_output_dir_caps_file_count(tmp_path: Path) -> None:
    """A check script writing an excessive number of output files must not force
    reading all of them into memory (docs/known_bugs.md #11)."""
    out = tmp_path / "out"
    out.mkdir()
    cap = 5
    for i in range(cap + 10):
        (out / f"f{i}.txt").write_text(str(i), encoding="utf-8")

    with patch("submissions_checker.services.docker_sandbox.MAX_OUTPUT_FILES", cap):
        files = DockerSandbox()._read_output_dir(out)

    assert len(files) == cap
