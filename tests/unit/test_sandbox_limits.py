"""Sandbox resource requests declared by a subject are bounded by a host maximum.

A subject's ``config.yml`` chooses its own sandbox ``memory`` and ``cpus``. Unbounded,
a subject declaring ``memory: 2g`` exhausts a small production host. These tests cover
the clamp, the log line it emits, and the fact that the standalone runner inherits the
same behaviour because both paths resolve their plan through the check core.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from submissions_checker.services import check_core
from submissions_checker.services.check_core import SandboxLimits


def _config(memory: str = "256m", cpus: float = 0.5) -> dict:
    return {
        "subjectCode": "demo",
        "assignments": {
            "lab1": {
                "sandbox": {
                    "image": "demo:local",
                    "memory": memory,
                    "cpus": cpus,
                    "check_command": "assignments/lab1/check.py",
                }
            }
        },
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("512", 512),
        ("64b", 64),
        ("100k", 100 * 1024),
        ("256m", 256 * 1024 * 1024),
        ("2g", 2 * 1024 * 1024 * 1024),
        ("1G", 1024 * 1024 * 1024),
    ],
)
def test_parse_memory_understands_docker_suffixes(raw: str, expected: int) -> None:
    assert check_core.parse_memory(raw) == expected


def test_unparseable_memory_falls_back_to_the_limit() -> None:
    """A malformed value must not become an unbounded request."""
    limits = SandboxLimits(max_memory="512m", max_cpus=2.0)
    plan = check_core.resolve_check_plan(_config(memory="lots"), "lab1", None, limits=limits)
    assert plan.memory == "512m"


def test_memory_over_the_limit_is_clamped_and_logged() -> None:
    limits = SandboxLimits(max_memory="512m", max_cpus=2.0)

    with capture_logs() as logs:
        plan = check_core.resolve_check_plan(_config(memory="2g"), "lab1", None, limits=limits)

    assert plan.memory == "512m"
    warning = next(entry for entry in logs if entry["event"] == "sandbox_memory_clamped")
    assert warning["log_level"] == "warning"
    assert warning["requested"] == "2g"
    assert warning["applied"] == "512m"
    assert warning["subject"] == "demo", "the subject must be identifiable from the warning"
    assert warning["assignment"] == "lab1"


def test_cpus_over_the_limit_is_clamped_and_logged() -> None:
    limits = SandboxLimits(max_memory="512m", max_cpus=1.0)

    with capture_logs() as logs:
        plan = check_core.resolve_check_plan(_config(cpus=4.0), "lab1", None, limits=limits)

    assert plan.cpus == 1.0
    warning = next(entry for entry in logs if entry["event"] == "sandbox_cpus_clamped")
    assert warning["requested"] == 4.0
    assert warning["applied"] == 1.0


def test_request_within_the_limit_passes_through_unchanged_and_unwarned() -> None:
    limits = SandboxLimits(max_memory="512m", max_cpus=2.0)

    with capture_logs() as logs:
        plan = check_core.resolve_check_plan(
            _config(memory="256m", cpus=0.5), "lab1", None, limits=limits
        )

    assert plan.memory == "256m"
    assert plan.cpus == 0.5
    assert not [entry for entry in logs if "clamped" in entry["event"]]


def test_request_exactly_at_the_limit_is_not_clamped() -> None:
    limits = SandboxLimits(max_memory="512m", max_cpus=2.0)

    with capture_logs() as logs:
        plan = check_core.resolve_check_plan(
            _config(memory="512m", cpus=2.0), "lab1", None, limits=limits
        )

    assert plan.memory == "512m"
    assert plan.cpus == 2.0
    assert not [entry for entry in logs if "clamped" in entry["event"]]


def test_defaults_apply_when_no_limits_are_configured(monkeypatch) -> None:
    """Subjects using ordinary settings must keep working with no configuration at all."""
    monkeypatch.delenv("SANDBOX_MAX_MEMORY", raising=False)
    monkeypatch.delenv("SANDBOX_MAX_CPUS", raising=False)

    plan = check_core.resolve_check_plan(_config(memory="256m", cpus=0.5), "lab1", None)

    assert plan.memory == "256m"
    assert plan.cpus == 0.5


def test_limits_are_read_from_the_environment_for_the_standalone_runner(monkeypatch) -> None:
    """The runner never instantiates Settings, so its bounds come from the environment."""
    monkeypatch.setenv("SANDBOX_MAX_MEMORY", "128m")
    monkeypatch.setenv("SANDBOX_MAX_CPUS", "0.25")

    limits = SandboxLimits.from_env()
    assert limits.max_memory == "128m"
    assert limits.max_cpus == 0.25

    plan = check_core.resolve_check_plan(_config(memory="1g", cpus=2.0), "lab1", None)
    assert plan.memory == "128m"
    assert plan.cpus == 0.25


def test_malformed_environment_limits_fall_back_to_defaults(monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_MAX_MEMORY", "not-a-size")
    monkeypatch.setenv("SANDBOX_MAX_CPUS", "not-a-number")

    limits = SandboxLimits.from_env()
    assert limits.max_memory == check_core.DEFAULT_MAX_MEMORY
    assert limits.max_cpus == check_core.DEFAULT_MAX_CPUS
