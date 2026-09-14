"""DB-free check core: resolve a check plan from config and run it in the sandbox.

This is the single source of truth for *how a submission is checked* — sandbox-block
resolution (common + variant merge), running `validate_command` then `check_command`(s),
parsing each `/output/result.json`, recomputing the score, and applying `min_pass_score`.

It has NO dependency on the database, outbox, state machine, AI, or storage. Both the
production worker (`workers/tasks/check_tasks.py`) and the standalone runner
(`cli/runner.py`) call into here, so there is exactly one check code path and no drift.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from submissions_checker.core.logging import get_logger
from submissions_checker.services.docker_sandbox import DockerSandbox, SandboxResult

logger = get_logger(__name__)

_DEFAULT_IMAGE = "python:3.12-slim"
_DEFAULT_TOOL = "python3"
_DEFAULT_MEMORY = "256m"
_DEFAULT_CPUS = 0.5
_DEFAULT_TIMEOUT = 30
_DEFAULT_MIN_PASS = 100

# Upper bounds on what a subject's config.yml may request. A subject picks its own
# sandbox size, so without a ceiling one subject declaring `memory: 2g` can exhaust a
# small host and take the platform down for every other subject. Sized for the smallest
# supported production host; override per host rather than editing these.
DEFAULT_MAX_MEMORY = "512m"
DEFAULT_MAX_CPUS = 1.0

_MEMORY_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_MEMORY_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([bkmg])?\s*$", re.IGNORECASE)


def parse_memory(value: str) -> int | None:
    """Parse a Docker-style memory string ("512", "100k", "256m", "2g") into bytes.

    Returns None for anything unparseable, so callers can decide what an unreadable
    request means rather than silently treating it as zero or unlimited.
    """
    match = _MEMORY_PATTERN.match(str(value))
    if not match:
        return None
    amount, unit = match.groups()
    return int(float(amount) * _MEMORY_UNITS[(unit or "b").lower()])


@dataclass(frozen=True)
class SandboxLimits:
    """Host-imposed ceiling on the sandbox resources a subject may request.

    Read from the environment rather than from Settings: the check core is shared with
    the standalone runner, which deliberately never instantiates Settings (it has no
    database URL or secret key). The application passes limits explicitly from its own
    configuration; the runner falls back to these environment variables.
    """

    max_memory: str = DEFAULT_MAX_MEMORY
    max_cpus: float = DEFAULT_MAX_CPUS

    @classmethod
    def from_env(cls) -> SandboxLimits:
        max_memory = os.environ.get("SANDBOX_MAX_MEMORY", DEFAULT_MAX_MEMORY)
        if parse_memory(max_memory) is None:
            logger.warning(
                "sandbox_max_memory_unparseable",
                value=max_memory,
                fallback=DEFAULT_MAX_MEMORY,
            )
            max_memory = DEFAULT_MAX_MEMORY

        raw_cpus = os.environ.get("SANDBOX_MAX_CPUS")
        try:
            max_cpus = float(raw_cpus) if raw_cpus is not None else DEFAULT_MAX_CPUS
        except ValueError:
            logger.warning(
                "sandbox_max_cpus_unparseable", value=raw_cpus, fallback=DEFAULT_MAX_CPUS
            )
            max_cpus = DEFAULT_MAX_CPUS

        return cls(max_memory=max_memory, max_cpus=max_cpus)

    def clamp_memory(self, requested: str, *, subject: str, assignment: str) -> str:
        """Bound a requested memory string, logging when the request is not honoured."""
        requested_bytes = parse_memory(requested)
        limit_bytes = parse_memory(self.max_memory) or parse_memory(DEFAULT_MAX_MEMORY)

        if requested_bytes is None:
            # An unreadable request must not become an unbounded one.
            logger.warning(
                "sandbox_memory_clamped",
                subject=subject,
                assignment=assignment,
                requested=requested,
                applied=self.max_memory,
                reason="unparseable",
            )
            return self.max_memory

        if limit_bytes is not None and requested_bytes > limit_bytes:
            logger.warning(
                "sandbox_memory_clamped",
                subject=subject,
                assignment=assignment,
                requested=requested,
                applied=self.max_memory,
                reason="exceeds_host_limit",
            )
            return self.max_memory

        return requested

    def clamp_cpus(self, requested: float, *, subject: str, assignment: str) -> float:
        """Bound a requested CPU share, logging when the request is not honoured."""
        if requested > self.max_cpus:
            logger.warning(
                "sandbox_cpus_clamped",
                subject=subject,
                assignment=assignment,
                requested=requested,
                applied=self.max_cpus,
                reason="exceeds_host_limit",
            )
            return self.max_cpus
        return requested


class SandboxRunner(Protocol):
    """The slice of DockerSandbox the core needs — lets tests inject a fake."""

    async def run(
        self,
        *,
        image: str,
        tool: str,
        script_path: str,
        student_files_dir: Path,
        plugin_dir: Path,
        env: dict[str, str] | None = ...,
        memory: str = ...,
        cpus: float = ...,
        timeout: int = ...,
    ) -> SandboxResult: ...


_DEFAULT_SANDBOX: DockerSandbox = DockerSandbox()


class CheckExecutionError(RuntimeError):
    """A technical sandbox failure (non-zero exit / missing result.json), not a low grade."""


@dataclass
class ConfigError:
    """The config could not be resolved into a runnable plan (misconfiguration)."""

    reason: str


@dataclass
class CheckPlan:
    """Fully-resolved instructions for checking one submission — no DB, no config dict."""

    image: str
    tool: str
    memory: str
    cpus: float
    timeout: int
    min_pass_score: int
    validate_command: str | None
    common_check: str | None
    variant_check: str | None
    variant: str | None


@dataclass
class CheckOutcome:
    """The result of checking one submission."""

    # one of: "passed" | "failed" | "validation_failed" | "config_error"
    status: str
    score: int
    max_score: int
    tests: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def resolve_check_plan(
    config: dict[str, Any],
    assignment_code: str,
    variant: str | None,
    *,
    limits: SandboxLimits | None = None,
) -> CheckPlan | ConfigError:
    """Resolve the effective sandbox plan for an assignment/variant from a subject config.

    Returns a ConfigError (not raising) for misconfiguration so callers can surface a
    teacher-facing reason. Mirrors the resolution that production has always used.

    Sandbox resource requests are bounded by ``limits`` (defaulting to the host's
    environment), so a subject cannot ask for more than the host can give.
    """
    bounds = limits if limits is not None else SandboxLimits.from_env()
    subject = str(config.get("subjectCode", "unknown"))
    assignments_config: dict[str, Any] = config.get("assignments", {})
    if not assignment_code or assignment_code not in assignments_config:
        return ConfigError("Assignment is not configured in plugin. Contact your teacher.")

    plugin_assignment: dict[str, Any] = assignments_config[assignment_code]

    # New common/variants structure vs old flat structure.
    common_cfg = plugin_assignment.get("common", {})
    if common_cfg:
        sandbox_cfg: dict[str, Any] = common_cfg.get("sandbox", {})
        variant_entry = (
            plugin_assignment.get("variants", {}).get(str(variant), {}) if variant else {}
        )
        variant_sandbox: dict[str, Any] = variant_entry.get("sandbox", {})
        validate_command: str | None = variant_sandbox.get("validate_command") or sandbox_cfg.get(
            "validate_command"
        )
        common_check: str | None = sandbox_cfg.get("check_command")
        variant_check: str | None = variant_sandbox.get("check_command")
    else:
        sandbox_cfg = plugin_assignment.get("sandbox", {})
        variant_overrides = (
            plugin_assignment.get("variants", {}).get(str(variant), {}) if variant else {}
        )
        validate_command = variant_overrides.get("validate_command") or sandbox_cfg.get(
            "validate_command"
        )
        common_check = None
        variant_check = variant_overrides.get("check_command") or sandbox_cfg.get("check_command")

    if plugin_assignment.get("variants_required") and not variant:
        return ConfigError(
            "Your variant has not been assigned yet. Contact your teacher to have your variant set."
        )

    if not common_check and not variant_check:
        return ConfigError("No check_command configured for this assignment. Contact your teacher.")

    return CheckPlan(
        image=sandbox_cfg.get("image", _DEFAULT_IMAGE),
        tool=sandbox_cfg.get("tool", _DEFAULT_TOOL),
        memory=bounds.clamp_memory(
            str(sandbox_cfg.get("memory", _DEFAULT_MEMORY)),
            subject=subject,
            assignment=assignment_code,
        ),
        cpus=bounds.clamp_cpus(
            float(sandbox_cfg.get("cpus", _DEFAULT_CPUS)),
            subject=subject,
            assignment=assignment_code,
        ),
        timeout=int(sandbox_cfg.get("timeout_seconds", _DEFAULT_TIMEOUT)),
        min_pass_score=int(sandbox_cfg.get("min_pass_score", _DEFAULT_MIN_PASS)),
        validate_command=validate_command,
        common_check=common_check,
        variant_check=variant_check,
        variant=str(variant) if variant else None,
    )


async def run_check(
    *,
    plan: CheckPlan,
    submission_dir: Path,
    plugin_dir: Path,
    sandbox: SandboxRunner | None = None,
) -> CheckOutcome:
    """Run a resolved plan against a submission and return the outcome.

    Runs `validate_command` first (short-circuits to validation_failed on non-zero exit),
    then the common and variant `check_command`s, recomputes the score from per-test
    points, and applies `min_pass_score`. Raises CheckExecutionError on technical failure.
    """
    sb = sandbox if sandbox is not None else _DEFAULT_SANDBOX
    env: dict[str, str] = {"VARIANT": plan.variant} if plan.variant else {}

    if plan.validate_command:
        vr = await sb.run(
            image=plan.image,
            tool=plan.tool,
            script_path=plan.validate_command,
            student_files_dir=submission_dir,
            plugin_dir=plugin_dir,
            env=env,
            memory=plan.memory,
            cpus=plan.cpus,
            timeout=plan.timeout,
        )
        if vr.exit_code != 0:
            reason = (
                vr.output_files.get("validate_error.txt")
                or vr.stderr.strip()
                or "Validation failed: submitted files do not meet requirements."
            )
            return CheckOutcome("validation_failed", 0, 0, [], reason.strip())

    all_tests: list[dict[str, Any]] = []
    for script in (plan.common_check, plan.variant_check):
        if script:
            all_tests.extend(
                await _run_one_check(sb, plan, script, submission_dir, plugin_dir, env)
            )

    total_score = sum(t.get("points_earned", int(bool(t.get("passed")))) for t in all_tests)
    max_score_total = sum(t.get("max_points", 1) for t in all_tests)
    passed = (
        (total_score / max_score_total * 100) >= plan.min_pass_score
        if max_score_total > 0
        else False
    )
    return CheckOutcome(
        status="passed" if passed else "failed",
        score=total_score,
        max_score=max_score_total,
        tests=all_tests,
    )


async def check_submission(
    *,
    config: dict[str, Any],
    assignment_code: str,
    variant: str | None,
    submission_dir: Path,
    plugin_dir: Path,
    sandbox: SandboxRunner | None = None,
) -> CheckOutcome:
    """Resolve + run in one call. Returns a config_error outcome for misconfiguration.

    Convenience entry point for the standalone runner; the production worker uses
    resolve_check_plan and run_check separately so it can map states precisely.
    """
    plan = resolve_check_plan(config, assignment_code, variant)
    if isinstance(plan, ConfigError):
        return CheckOutcome("config_error", 0, 0, [], plan.reason)
    return await run_check(
        plan=plan, submission_dir=submission_dir, plugin_dir=plugin_dir, sandbox=sandbox
    )


async def _run_one_check(
    sandbox: SandboxRunner,
    plan: CheckPlan,
    script_path: str,
    submission_dir: Path,
    plugin_dir: Path,
    env: dict[str, str],
) -> list[dict[str, Any]]:
    """Run one check script and return its test list. Raises on technical failure."""
    result = await sandbox.run(
        image=plan.image,
        tool=plan.tool,
        script_path=script_path,
        student_files_dir=submission_dir,
        plugin_dir=plugin_dir,
        env=env,
        memory=plan.memory,
        cpus=plan.cpus,
        timeout=plan.timeout,
    )
    if result.exit_code != 0:
        raise CheckExecutionError(
            f"Sandbox technical failure running {script_path} "
            f"(exit {result.exit_code}): {result.stderr[:500]}"
        )
    raw = result.output_files.get("result.json")
    if not raw:
        raise CheckExecutionError(f"Check script {script_path} did not write /output/result.json")
    try:
        parsed: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckExecutionError(
            f"result.json from {script_path} is not valid JSON: {exc}"
        ) from exc
    tests = parsed.get("tests", [])
    if not isinstance(tests, list):
        raise CheckExecutionError(f"result.json from {script_path} has a non-list 'tests' key")
    return tests
