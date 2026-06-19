"""Runner CLI test: drive `run-suite` against a tiny fixture subject.

The real sandbox is replaced with a fake (no Docker) so this runs anywhere. It exercises
suite parsing, path resolution, per-case assertion, and the process exit code.
"""

from __future__ import annotations

import json
import textwrap

from submissions_checker.cli import runner
from submissions_checker.services import check_core
from submissions_checker.services.docker_sandbox import SandboxResult


class _FakeSandbox:
    """Full marks for fixtures under correct/, zero for wrong/."""

    async def run(self, *, image, tool, script_path, student_files_dir, plugin_dir,
                  env=None, memory="256m", cpus=0.5, timeout=30):
        good = "correct" in str(student_files_dir)
        pts = 100 if good else 0
        tests = [{"name": "t", "passed": good, "points_earned": pts, "max_points": 100}]
        return SandboxResult(0, "", "", {"result.json": json.dumps({"tests": tests})})


def _make_subject(root) -> None:
    (root / "config.yml").write_text(textwrap.dedent("""
        subjectCode: demo
        name: Demo
        assignments:
          lab1:
            sandbox:
              image: demo:local
              check_command: assignments/lab1/check.py
              min_pass_score: 50
    """).strip() + "\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "suite.yml").write_text(textwrap.dedent("""
        cases:
          - assignment: lab1
            submission: tests/fixtures/correct
            expect: pass
            expect_score: 100
          - assignment: lab1
            submission: tests/fixtures/wrong
            expect: fail
    """).strip() + "\n", encoding="utf-8")
    for kind in ("correct", "wrong"):
        d = root / "tests" / "fixtures" / kind
        d.mkdir(parents=True)
        (d / "solution.py").write_text("print('x')\n", encoding="utf-8")


def test_run_suite_all_match(tmp_path, monkeypatch, capsys) -> None:
    _make_subject(tmp_path)
    monkeypatch.setattr(check_core, "_DEFAULT_SANDBOX", _FakeSandbox())

    code = runner.main(["run-suite", str(tmp_path / "tests" / "suite.yml")])

    out = capsys.readouterr().out
    assert code == runner.EXIT_OK
    assert "2/2 cases matched" in out


def test_run_suite_reports_mismatch(tmp_path, monkeypatch) -> None:
    _make_subject(tmp_path)
    # Flip expectation so the correct fixture is (wrongly) expected to fail.
    suite = tmp_path / "tests" / "suite.yml"
    suite.write_text(suite.read_text().replace("expect: pass", "expect: fail"), encoding="utf-8")
    monkeypatch.setattr(check_core, "_DEFAULT_SANDBOX", _FakeSandbox())

    code = runner.main(["run-suite", str(suite)])
    assert code == runner.EXIT_MISMATCH


def test_run_suite_missing_file_is_usage_error(tmp_path) -> None:
    code = runner.main(["run-suite", str(tmp_path / "nope.yml")])
    assert code == runner.EXIT_USAGE
