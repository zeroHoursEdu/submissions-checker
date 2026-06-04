"""Submission checking tasks — runs student code against plugin tests in Docker sandbox."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.core.state_machine import transition
from submissions_checker.db.models import (
    OutboxMessage,
    StudentAssignment,
    SubjectsAssignment,
    Subject,
    Submission,
    SubjectPluginConfig,
)
from submissions_checker.db.models.enums import OutboxEventType, OutboxMessageState
from submissions_checker.services.docker_sandbox import DockerSandbox, SandboxResult

logger = get_logger(__name__)

UPLOADS_DIR = Path("uploads")
_SANDBOX = DockerSandbox()


async def execute_check_task(db: AsyncSession, payload: dict[str, Any]) -> None:
    submission_id: int = payload["submission_id"]

    result = await db.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .options(
            selectinload(Submission.students_assignment)
            .selectinload(StudentAssignment.subjects_assignment)
            .selectinload(SubjectsAssignment.subject)
        )
    )
    submission = result.scalar_one_or_none()
    if submission is None:
        logger.error("check_task_submission_not_found", submission_id=submission_id)
        return

    student_assignment = submission.students_assignment
    subjects_assignment = student_assignment.subjects_assignment
    subject = subjects_assignment.subject

    # Pin config version if not already pinned (only pin once when check starts)
    if submission.plugin_config_id is None:
        config_record = await _fetch_latest_config(db, subject.id)
        if config_record is None:
            _fail_validation(submission, "No plugin configuration found for this subject. Contact your teacher.")
            return
        submission.plugin_config_id = config_record.id
    else:
        config_record = await db.get(SubjectPluginConfig, submission.plugin_config_id)
        if config_record is None:
            raise RuntimeError(f"Pinned plugin_config_id={submission.plugin_config_id} not found")

    assignments_config: dict[str, Any] = config_record.config.get("assignments", {})
    assignment_code = subjects_assignment.code
    if not assignment_code or assignment_code not in assignments_config:
        _fail_validation(submission, "Assignment is not configured in plugin. Contact your teacher.")
        return

    plugin_assignment: dict[str, Any] = assignments_config[assignment_code]
    variant = student_assignment.variant

    # ── Resolve config structure (new common/variants vs old flat) ─────────────
    common_cfg = plugin_assignment.get("common", {})
    if common_cfg:
        sandbox_cfg: dict[str, Any] = common_cfg.get("sandbox", {})
        variant_entry = plugin_assignment.get("variants", {}).get(str(variant), {}) if variant else {}
        variant_sandbox: dict[str, Any] = variant_entry.get("sandbox", {})
        validate_command: str | None = variant_sandbox.get("validate_command") or sandbox_cfg.get("validate_command")
        common_check: str | None = sandbox_cfg.get("check_command")
        variant_check: str | None = variant_sandbox.get("check_command")
    else:
        sandbox_cfg = plugin_assignment.get("sandbox", {})
        variant_overrides = plugin_assignment.get("variants", {}).get(str(variant), {}) if variant else {}
        validate_command = variant_overrides.get("validate_command") or sandbox_cfg.get("validate_command")
        common_check = None
        variant_check = variant_overrides.get("check_command") or sandbox_cfg.get("check_command")

    # Variant check — must happen before sandbox
    if plugin_assignment.get("variants_required") and not variant:
        _fail_validation(
            submission,
            "Your variant has not been assigned yet. Contact your teacher to have your variant set.",
        )
        return

    if not common_check and not variant_check:
        _fail_validation(submission, "No check_command configured for this assignment. Contact your teacher.")
        return

    image: str = sandbox_cfg.get("image", "python:3.12-slim")
    tool: str = sandbox_cfg.get("tool", "python3")
    memory: str = sandbox_cfg.get("memory", "256m")
    cpus: float = float(sandbox_cfg.get("cpus", 0.5))
    timeout: int = int(sandbox_cfg.get("timeout_seconds", 30))
    min_pass_score: int = int(sandbox_cfg.get("min_pass_score", 100))

    settings = get_settings()
    plugin_dir = Path(settings.plugins_dir) / (config_record.config.get("subjectCode") or "")

    # Locate and extract the submitted ZIP
    saved_as = (submission.source_metadata or {}).get("saved_as")
    if not saved_as:
        raise RuntimeError("Submission has no saved_as in source_metadata")
    zip_path = UPLOADS_DIR / saved_as

    with tempfile.TemporaryDirectory(prefix="submission_") as extract_dir:
        extract_path = Path(extract_dir)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_path)
        except (zipfile.BadZipFile, OSError) as exc:
            _fail_validation(submission, f"Could not open submitted ZIP: {exc}")
            return

        env: dict[str, str] = {}
        if variant:
            env["VARIANT"] = str(variant)

        # ── Validation step ────────────────────────────────────────────────────
        transition(submission, "start_validation")

        if validate_command:
            validate_result = await _SANDBOX.run(
                image=image, tool=tool, script_path=validate_command,
                student_files_dir=extract_path, plugin_dir=plugin_dir,
                env=env, memory=memory, cpus=cpus, timeout=timeout,
            )
            if validate_result.exit_code != 0:
                reason = (
                    validate_result.output_files.get("validate_error.txt")
                    or validate_result.stderr.strip()
                    or "Validation failed: submitted files do not meet requirements."
                )
                _fail_validation(submission, reason.strip())
                return

        transition(submission, "validation_passed")

        # ── Testing step — run common check then variant check, merge results ──
        all_tests: list[dict[str, Any]] = []

        if common_check:
            common_result = await _run_check(
                common_check, image, tool, extract_path, plugin_dir, env, memory, cpus, timeout
            )
            all_tests.extend(common_result)

        if variant_check:
            variant_result = await _run_check(
                variant_check, image, tool, extract_path, plugin_dir, env, memory, cpus, timeout
            )
            all_tests.extend(variant_result)

        # Recompute score from merged tests (script-provided totals are ignored)
        total_score = sum(
            t.get("points_earned", int(bool(t.get("passed")))) for t in all_tests
        )
        max_score_total = sum(t.get("max_points", 1) for t in all_tests)
        passed = (
            (total_score / max_score_total * 100) >= min_pass_score
            if max_score_total > 0
            else False
        )

        submission.test_results = {
            "passed": passed,
            "score": total_score,
            "max_score": max_score_total,
            "tests": all_tests,
            "plugin_config_version": config_record.version,
        }

        if not passed:
            transition(submission, "test_failed")
            return

        review_mode: str = plugin_assignment.get("review_mode", "tests_only")
        _advance_after_tests(db, submission, review_mode)


async def _run_check(
    script_path: str,
    image: str,
    tool: str,
    student_files_dir: Path,
    plugin_dir: Path,
    env: dict[str, str],
    memory: str,
    cpus: float,
    timeout: int,
) -> list[dict[str, Any]]:
    """Run one check script and return its test list. Raises RuntimeError on technical failure."""
    result: SandboxResult = await _SANDBOX.run(
        image=image, tool=tool, script_path=script_path,
        student_files_dir=student_files_dir, plugin_dir=plugin_dir,
        env=env, memory=memory, cpus=cpus, timeout=timeout,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"Sandbox technical failure running {script_path} "
            f"(exit {result.exit_code}): {result.stderr[:500]}"
        )
    result_raw = result.output_files.get("result.json")
    if not result_raw:
        raise RuntimeError(f"Check script {script_path} did not write /output/result.json")
    try:
        parsed: dict[str, Any] = json.loads(result_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"result.json from {script_path} is not valid JSON: {exc}") from exc
    return parsed.get("tests", [])


async def _fetch_latest_config(db: AsyncSession, subject_id: int) -> SubjectPluginConfig | None:
    result = await db.execute(
        select(SubjectPluginConfig)
        .where(SubjectPluginConfig.subject_id == subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _fail_validation(submission: Submission, reason: str) -> None:
    submission.test_results = {"check_reason": reason}
    transition(submission, "validation_failed")


def _advance_after_tests(db: AsyncSession, submission: Submission, review_mode: str) -> None:
    if review_mode == "tests_then_ai":
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(
            event_type=OutboxEventType.RUN_AI_REVIEW,
            state=OutboxMessageState.PENDING,
            payload={"submission_id": submission.id},
        ))
    elif review_mode == "tests_then_teacher":
        transition(submission, "test_passed_teacher")
    elif review_mode in ("tests_then_ai_then_teacher", "tests_then_ai_teacher"):
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(
            event_type=OutboxEventType.RUN_AI_REVIEW,
            state=OutboxMessageState.PENDING,
            payload={"submission_id": submission.id, "next_step": "teacher"},
        ))
    elif review_mode == "tests_then_quiz":
        transition(submission, "test_passed_quiz")
    else:
        transition(submission, "test_passed_tests_only")
