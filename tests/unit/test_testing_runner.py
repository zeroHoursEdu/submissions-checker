"""Contract tests for skeleton TestRunner and the submission_checker stub.

TestRunner methods raise NotImplementedError (no subprocess is ever spawned by
the skeleton). check_submission is a stub that currently always passes. These
tests pin both contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from submissions_checker.services.submission_checker import check_submission
from submissions_checker.services.testing.runner import TestRunner

# ── TestRunner skeleton ────────────────────────────────────────────────────────


async def test_run_tests_not_implemented() -> None:
    runner = TestRunner()
    with pytest.raises(NotImplementedError):
        await runner.run_tests(Path("/tmp/repo"), "pytest", timeout=10)


async def test_run_tests_default_timeout_not_implemented() -> None:
    runner = TestRunner()
    with pytest.raises(NotImplementedError):
        await runner.run_tests(Path("/tmp/repo"), "pytest")


async def test_install_dependencies_not_implemented() -> None:
    runner = TestRunner()
    with pytest.raises(NotImplementedError):
        await runner.install_dependencies(Path("/tmp/repo"))


# ── submission_checker stub ────────────────────────────────────────────────────


def test_check_submission_currently_always_passes(tmp_path: Path) -> None:
    # NOTE: stub implementation — always returns (True, "") regardless of input.
    zip_path = tmp_path / "submission.zip"
    zip_path.write_bytes(b"not really a zip")
    passed, reason = check_submission(zip_path)
    assert passed is True
    assert reason == ""


def test_check_submission_passed_reason_empty_on_pass() -> None:
    passed, reason = check_submission(Path("/nonexistent.zip"))
    assert passed is True
    assert reason == ""
