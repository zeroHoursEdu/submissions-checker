"""Extra runner CLI coverage — single-run command, JSON output, and error paths.

Complements ``test_check_runner_cli.py`` (which drives ``run-suite`` happy/mismatch
paths). Here we cover the ``run`` subcommand, JSON output, ``_evaluate``'s
config-error / invalid-expect / expect-score branches, suite validation errors,
the ``root`` override in ``_resolve_root``, and ``main``'s top-level exception
handling (``CheckExecutionError`` -> EXIT_ERROR, ``FileNotFoundError`` -> EXIT_USAGE).
The check engine is patched so no Docker/sandbox runs.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from submissions_checker.cli import runner
from submissions_checker.services import check_core


# --------------------------------------------------------------------------- #
# _resolve_root
# --------------------------------------------------------------------------- #
def test_resolve_root_default_is_grandparent(tmp_path) -> None:
    suite = tmp_path / "tests" / "suite.yml"
    suite.parent.mkdir()
    assert runner._resolve_root(suite, None) == tmp_path.resolve()


def test_resolve_root_honours_override(tmp_path) -> None:
    suite = tmp_path / "tests" / "suite.yml"
    suite.parent.mkdir()
    got = runner._resolve_root(suite, "..")
    assert got == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# _evaluate branches (config_error / invalid expect / expect_score mismatch)
# --------------------------------------------------------------------------- #
def _patch_outcome(monkeypatch, outcome: check_core.CheckOutcome) -> None:
    async def fake_check_submission(**kwargs):
        return outcome

    monkeypatch.setattr(check_core, "check_submission", fake_check_submission)


def test_evaluate_config_error(monkeypatch, tmp_path) -> None:
    _patch_outcome(
        monkeypatch,
        check_core.CheckOutcome("config_error", 0, 0, [], "bad config"),
    )
    res = runner._evaluate(
        label="c",
        config={},
        assignment="lab1",
        variant=None,
        submission_dir=tmp_path,
        plugin_dir=tmp_path,
        expect="pass",
        expect_score=None,
    )
    assert res.ok is False
    assert "config error" in res.detail


def test_evaluate_invalid_expect(monkeypatch, tmp_path) -> None:
    _patch_outcome(monkeypatch, check_core.CheckOutcome("passed", 100, 100, []))
    res = runner._evaluate(
        label="c",
        config={},
        assignment="lab1",
        variant=None,
        submission_dir=tmp_path,
        plugin_dir=tmp_path,
        expect="maybe",
        expect_score=None,
    )
    assert res.ok is False
    assert "invalid expect" in res.detail


def test_evaluate_expect_score_mismatch(monkeypatch, tmp_path) -> None:
    _patch_outcome(monkeypatch, check_core.CheckOutcome("passed", 80, 100, []))
    res = runner._evaluate(
        label="c",
        config={},
        assignment="lab1",
        variant=None,
        submission_dir=tmp_path,
        plugin_dir=tmp_path,
        expect="pass",
        expect_score=100,
    )
    assert res.ok is False
    assert "expected score 100" in res.detail


def test_evaluate_pass_matches_with_score(monkeypatch, tmp_path) -> None:
    _patch_outcome(monkeypatch, check_core.CheckOutcome("passed", 100, 100, []))
    res = runner._evaluate(
        label="c",
        config={},
        assignment="lab1",
        variant="3",
        submission_dir=tmp_path,
        plugin_dir=tmp_path,
        expect="pass",
        expect_score=100,
    )
    assert res.ok is True


# --------------------------------------------------------------------------- #
# _cmd_run (single submission) + JSON output
# --------------------------------------------------------------------------- #
def _write_config(root: Path) -> Path:
    cfg = root / "config.yml"
    cfg.write_text(
        textwrap.dedent("""
        subjectCode: demo
        name: Demo
        assignments:
          lab1:
            sandbox:
              image: demo:local
              check_command: assignments/lab1/check.py
              min_pass_score: 50
    """).strip()
        + "\n",
        encoding="utf-8",
    )
    return cfg


def test_cmd_run_human_output_ok(tmp_path, monkeypatch, capsys) -> None:
    _write_config(tmp_path)
    sub = tmp_path / "fix"
    sub.mkdir()
    _patch_outcome(monkeypatch, check_core.CheckOutcome("passed", 100, 100, []))

    code = runner.main(
        [
            "run",
            "--config",
            str(tmp_path / "config.yml"),
            "--assignment",
            "lab1",
            "--submission",
            str(sub),
            "--expect",
            "pass",
        ]
    )

    out = capsys.readouterr().out
    assert code == runner.EXIT_OK
    assert "[PASS]" in out
    assert "1/1 cases matched" in out


def test_cmd_run_json_output_mismatch(tmp_path, monkeypatch, capsys) -> None:
    _write_config(tmp_path)
    sub = tmp_path / "fix"
    sub.mkdir()
    _patch_outcome(monkeypatch, check_core.CheckOutcome("failed", 0, 100, []))

    code = runner.main(
        [
            "--json",
            "run",
            "--config",
            str(tmp_path / "config.yml"),
            "--assignment",
            "lab1",
            "--variant",
            "3",
            "--submission",
            str(sub),
            "--expect",
            "pass",
        ]
    )

    out = capsys.readouterr().out
    assert code == runner.EXIT_MISMATCH
    payload = json.loads(out)
    assert payload["total"] == 1
    assert payload["matched"] == 0
    assert payload["cases"][0]["ok"] is False
    assert payload["cases"][0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# run-suite validation errors
# --------------------------------------------------------------------------- #
def test_run_suite_not_a_mapping_with_cases(tmp_path, capsys) -> None:
    suite = tmp_path / "suite.yml"
    suite.write_text("just_a_string\n", encoding="utf-8")
    code = runner.main(["run-suite", str(suite)])
    err = capsys.readouterr().err
    assert code == runner.EXIT_USAGE
    assert "must be a mapping with a 'cases' list" in err


def test_run_suite_missing_config(tmp_path, capsys) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "suite.yml").write_text(
        "cases:\n  - assignment: lab1\n    submission: x\n    expect: pass\n",
        encoding="utf-8",
    )
    # No config.yml at repo root -> EXIT_USAGE.
    code = runner.main(["run-suite", str(tests / "suite.yml")])
    err = capsys.readouterr().err
    assert code == runner.EXIT_USAGE
    assert "config not found" in err


def test_run_suite_case_missing_keys(tmp_path, capsys) -> None:
    _write_config(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "suite.yml").write_text(
        textwrap.dedent("""
            cases:
              - assignment: lab1
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    code = runner.main(["run-suite", str(tests / "suite.yml")])
    err = capsys.readouterr().err
    assert code == runner.EXIT_USAGE
    assert "missing required keys" in err


def test_run_suite_fixture_missing_reports_config_error(tmp_path, monkeypatch, capsys) -> None:
    _write_config(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "suite.yml").write_text(
        textwrap.dedent("""
            cases:
              - assignment: lab1
                submission: tests/fixtures/ghost
                expect: pass
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    code = runner.main(["run-suite", str(tests / "suite.yml")])
    out = capsys.readouterr().out
    # Missing fixture -> a synthetic config_error case that fails to match.
    assert code == runner.EXIT_MISMATCH
    assert "fixture not found" in out


# --------------------------------------------------------------------------- #
# main() top-level error handling
# --------------------------------------------------------------------------- #
class _StubParser:
    """A minimal stand-in for the argparse parser whose ``parse_args`` returns a
    namespace whose ``func`` raises a chosen exception."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def parse_args(self, argv=None):
        import argparse

        def func(args):
            raise self._exc

        return argparse.Namespace(func=func)


def test_main_handles_check_execution_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        runner,
        "build_parser",
        lambda: _StubParser(check_core.CheckExecutionError("docker down")),
    )
    code = runner.main([])
    err = capsys.readouterr().err
    assert code == runner.EXIT_ERROR
    assert "sandbox technical failure" in err


def test_main_handles_file_not_found(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        runner,
        "build_parser",
        lambda: _StubParser(FileNotFoundError("config.yml")),
    )
    code = runner.main([])
    err = capsys.readouterr().err
    assert code == runner.EXIT_USAGE
    assert "error:" in err
