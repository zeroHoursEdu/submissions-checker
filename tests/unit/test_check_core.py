"""Unit tests for the DB-free check core — resolution, scoring, threshold, short-circuit.

No database and no Docker: a FakeSandbox returns canned SandboxResults so the scoring and
control flow are exercised in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from submissions_checker.services import check_core
from submissions_checker.services.docker_sandbox import SandboxResult

COMMON_CONFIG = {
    "subjectCode": "demo",
    "assignments": {
        "lab1": {
            "variants_required": True,
            "common": {
                "sandbox": {
                    "image": "demo-checker:local",
                    "tool": "python3",
                    "memory": "512m",
                    "cpus": 0.5,
                    "timeout_seconds": 30,
                    "min_pass_score": 60,
                    "validate_command": "assignments/validate.py",
                    "check_command": "assignments/lab1/check_common.py",
                }
            },
            "variants": {
                "3": {"sandbox": {"check_command": "assignments/lab1/check.py"}},
            },
        }
    },
}

FLAT_CONFIG = {
    "subjectCode": "demo",
    "assignments": {
        "lab1": {
            "sandbox": {
                "image": "demo:local",
                "check_command": "assignments/lab1/check.py",
                "min_pass_score": 50,
            }
        }
    },
}


class FakeSandbox:
    """Records each run and returns scripted SandboxResults keyed by script suffix."""

    def __init__(self, scripted: dict[str, SandboxResult]):
        self.scripted = scripted
        self.calls: list[dict] = []

    async def run(
        self,
        *,
        image,
        tool,
        script_path,
        student_files_dir,
        plugin_dir,
        env=None,
        memory="256m",
        cpus=0.5,
        timeout=30,
    ):
        self.calls.append({"script": script_path, "image": image, "env": dict(env or {})})
        for suffix, result in self.scripted.items():
            if script_path.endswith(suffix):
                return result
        raise AssertionError(f"no scripted result for {script_path}")


def _result_json(tests: list[dict], exit_code: int = 0) -> SandboxResult:
    return SandboxResult(
        exit_code=exit_code,
        stdout="",
        stderr="",
        output_files={"result.json": json.dumps({"tests": tests})},
    )


def test_resolve_merges_common_and_variant() -> None:
    plan = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", "3")
    assert isinstance(plan, check_core.CheckPlan)
    # image/limits/threshold come from common; check_command from the variant.
    assert plan.image == "demo-checker:local"
    assert plan.min_pass_score == 60
    assert plan.timeout == 30
    assert plan.validate_command == "assignments/validate.py"
    assert plan.common_check == "assignments/lab1/check_common.py"
    assert plan.variant_check == "assignments/lab1/check.py"
    assert plan.variant == "3"


def test_resolve_flat_structure() -> None:
    plan = check_core.resolve_check_plan(FLAT_CONFIG, "lab1", None)
    assert isinstance(plan, check_core.CheckPlan)
    assert plan.common_check is None
    assert plan.variant_check == "assignments/lab1/check.py"
    assert plan.min_pass_score == 50


def test_resolve_missing_assignment_is_config_error() -> None:
    err = check_core.resolve_check_plan(COMMON_CONFIG, "nope", "3")
    assert isinstance(err, check_core.ConfigError)


def test_resolve_variant_required_but_missing() -> None:
    err = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", None)
    assert isinstance(err, check_core.ConfigError)
    assert "variant" in err.reason.lower()


def test_resolve_no_check_command() -> None:
    cfg = {"assignments": {"lab1": {"sandbox": {"image": "x"}}}}
    err = check_core.resolve_check_plan(cfg, "lab1", None)
    assert isinstance(err, check_core.ConfigError)


async def test_run_check_passes_above_threshold() -> None:
    sandbox = FakeSandbox(
        {
            "validate.py": SandboxResult(0, "", "", {}),
            "check_common.py": _result_json(
                [
                    {"name": "c1", "passed": True, "points_earned": 40, "max_points": 40},
                ]
            ),
            "check.py": _result_json(
                [
                    {"name": "v1", "passed": True, "points_earned": 60, "max_points": 60},
                ]
            ),
        }
    )
    plan = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", "3")
    outcome = await check_core.run_check(
        plan=plan,
        submission_dir=Path("/tmp/x"),
        plugin_dir=Path("/tmp/p"),
        sandbox=sandbox,
    )
    assert outcome.passed
    assert outcome.score == 100
    assert outcome.max_score == 100
    # VARIANT env propagated to the sandbox.
    assert all(c["env"].get("VARIANT") == "3" for c in sandbox.calls)


async def test_run_check_fails_below_threshold() -> None:
    sandbox = FakeSandbox(
        {
            "validate.py": SandboxResult(0, "", "", {}),
            "check_common.py": _result_json(
                [
                    {"name": "c1", "passed": False, "points_earned": 0, "max_points": 40},
                ]
            ),
            "check.py": _result_json(
                [
                    {"name": "v1", "passed": True, "points_earned": 30, "max_points": 60},
                ]
            ),
        }
    )
    plan = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", "3")
    outcome = await check_core.run_check(
        plan=plan,
        submission_dir=Path("/tmp/x"),
        plugin_dir=Path("/tmp/p"),
        sandbox=sandbox,
    )
    # 30/100 = 30% < 60% threshold
    assert not outcome.passed
    assert outcome.status == "failed"
    assert outcome.score == 30


async def test_validation_failure_short_circuits() -> None:
    sandbox = FakeSandbox(
        {
            "validate.py": SandboxResult(1, "", "bad submission", {}),
            # check scripts must NOT run
        }
    )
    plan = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", "3")
    outcome = await check_core.run_check(
        plan=plan,
        submission_dir=Path("/tmp/x"),
        plugin_dir=Path("/tmp/p"),
        sandbox=sandbox,
    )
    assert outcome.status == "validation_failed"
    assert outcome.reason == "bad submission"
    assert [c["script"] for c in sandbox.calls] == ["assignments/validate.py"]


async def test_validation_error_file_preferred() -> None:
    sandbox = FakeSandbox(
        {
            "validate.py": SandboxResult(
                2, "", "stderr noise", {"validate_error.txt": "solution.py missing"}
            ),
        }
    )
    plan = check_core.resolve_check_plan(COMMON_CONFIG, "lab1", "3")
    outcome = await check_core.run_check(
        plan=plan,
        submission_dir=Path("/tmp/x"),
        plugin_dir=Path("/tmp/p"),
        sandbox=sandbox,
    )
    assert outcome.reason == "solution.py missing"


async def test_technical_failure_raises() -> None:
    sandbox = FakeSandbox(
        {
            "check.py": SandboxResult(137, "", "OOM killed", {}),
        }
    )
    plan = check_core.resolve_check_plan(FLAT_CONFIG, "lab1", None)
    with pytest.raises(check_core.CheckExecutionError):
        await check_core.run_check(
            plan=plan,
            submission_dir=Path("/tmp/x"),
            plugin_dir=Path("/tmp/p"),
            sandbox=sandbox,
        )


async def test_check_submission_reports_config_error() -> None:
    outcome = await check_core.check_submission(
        config=COMMON_CONFIG,
        assignment_code="missing",
        variant=None,
        submission_dir=Path("/tmp/x"),
        plugin_dir=Path("/tmp/p"),
    )
    assert outcome.status == "config_error"


def test_resolve_rejects_identical_common_and_variant_scripts() -> None:
    cfg = {
        "assignments": {
            "lab6": {
                "common": {"sandbox": {"image": "x", "check_command": "assignments/lab6/check.py"}},
                "variants": {"3": {"sandbox": {"check_command": "assignments/lab6/check.py"}}},
            }
        }
    }
    err = check_core.resolve_check_plan(cfg, "lab6", "3")
    assert isinstance(err, check_core.ConfigError)
    assert "same check_command" in err.reason
