"""Submission checking tasks — runs student code against plugin tests in Docker sandbox.

The actual checking (sandbox-block resolution, running validate/check scripts, scoring) lives
in the DB-free `services.check_core` so production and the standalone runner share one path.
This task is the persistence wrapper: load from DB, call the core, map the result to state
transitions and outbox messages.
"""

from __future__ import annotations

import os
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
    SubjectPluginConfig,
    SubjectsAssignment,
    Submission,
    User,
)
from submissions_checker.db.models.enums import (
    OutboxEventType,
    OutboxMessageState,
    SubmissionStatus,
)
from submissions_checker.services import check_core
from submissions_checker.services.docker_sandbox import DockerSandbox
from submissions_checker.services.grading import finalize_grade
from submissions_checker.services.notification_service import push_notification
from submissions_checker.utils.safe_zip import UnsafeArchiveError, safe_extract
from submissions_checker.workers.tasks.notification_tasks import enqueue_teacher_review_notification

logger = get_logger(__name__)

UPLOADS_DIR = Path("uploads")
_SANDBOX = DockerSandbox()

# Review modes that examine the student by quiz instead of by automated tests. Assignments in
# these modes need no `sandbox`/`check_command` block at all: the upload is accepted after an
# archive-safety check and the submission goes straight to the quiz.
_QUIZ_FIRST_MODES = frozenset({"quiz_only", "quiz_then_teacher"})


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

    assignment_code = subjects_assignment.code
    variant = student_assignment.variant
    plugin_assignment: dict[str, Any] = config_record.config.get("assignments", {}).get(
        assignment_code, {}
    )

    review_mode: str = plugin_assignment.get("review_mode", "tests_only")

    # Quiz-examined assignments never touch the sandbox — bail out before a check plan is even
    # resolved, since these configs legitimately carry no check_command.
    if review_mode in _QUIZ_FIRST_MODES:
        _accept_without_checks(submission, review_mode)
        return

    # Resolve the check plan from config (DB-free core). Misconfiguration → validation fail.
    plan = check_core.resolve_check_plan(config_record.config, assignment_code, variant)
    if isinstance(plan, check_core.ConfigError):
        _fail_validation(submission, plan.reason)
        return

    settings = get_settings()
    plugins_root = settings.host_plugins_dir or settings.plugins_dir
    plugin_dir = Path(plugins_root) / (config_record.config.get("subjectCode") or "")

    # Locate and extract the submitted ZIP
    saved_as = (submission.source_metadata or {}).get("saved_as")
    if not saved_as:
        raise RuntimeError("Submission has no saved_as in source_metadata")
    zip_path = UPLOADS_DIR / saved_as

    with tempfile.TemporaryDirectory(prefix="submission_") as extract_dir:
        extract_path = Path(extract_dir)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                safe_extract(zf, extract_path)
        except (zipfile.BadZipFile, OSError) as exc:
            _fail_validation(submission, f"Could not open submitted ZIP: {exc}")
            return
        except UnsafeArchiveError as exc:
            logger.error("check_task_unsafe_archive", submission_id=submission_id, error=str(exc))
            _fail_validation(submission, f"Submitted ZIP archive is unsafe: {exc}")
            return

        # Subject images drop to a non-root user (e.g. uid 10001), so every extracted file
        # and directory must be traversable/readable by that user, regardless of the mode
        # bits stored in the archive entries — mirrors the /output widening below.
        os.chmod(extract_path, 0o755)
        for root, dirs, files in os.walk(extract_path):
            for name in dirs:
                os.chmod(Path(root) / name, 0o755)
            for name in files:
                os.chmod(Path(root) / name, 0o644)

        # ── Validation + testing run in the shared core ────────────────────────
        transition(submission, "start_validation")
        try:
            outcome = await check_core.run_check(
                plan=plan, submission_dir=extract_path, plugin_dir=plugin_dir, sandbox=_SANDBOX
            )
        except check_core.CheckExecutionError as exc:
            logger.error("check_task_execution_error", submission_id=submission_id, error=str(exc))
            _fail_validation(submission, str(exc))
            return

        if outcome.status == "validation_failed":
            submission.test_results = {"check_reason": outcome.reason}
            transition(submission, "validation_failed")
            return

        transition(submission, "validation_passed")
        submission.test_results = {
            "passed": outcome.passed,
            "score": outcome.score,
            "max_score": outcome.max_score,
            "tests": outcome.tests,
            "plugin_config_version": config_record.version,
        }

        if not outcome.passed:
            transition(submission, "test_failed")
            await _notify_student(
                db, student_assignment.student_id,
                title=f"{subjects_assignment.title}: didn't pass",
                body=f"Your submission for \"{subjects_assignment.title}\" did not pass the automated checks.",
                link=f"/portal/subjects/{subject.id}/assignments/{subjects_assignment.id}",
            )
            return

        await _advance_after_tests(db, submission, review_mode)


async def _notify_student(db: AsyncSession, student_id: int, title: str, body: str, link: str) -> None:
    """Push an in-app notification for a graded submission. No-op if the student
    has no user account (shouldn't happen in practice, but never worth crashing
    the check task over)."""
    user_id = await db.scalar(select(User.id).where(User.student_id == student_id))
    if user_id is not None:
        await push_notification(db, user_id, title, body, link)


async def _fetch_latest_config(db: AsyncSession, subject_id: int) -> SubjectPluginConfig | None:
    result = await db.execute(
        select(SubjectPluginConfig)
        .where(SubjectPluginConfig.subject_id == subject_id)
        .order_by(SubjectPluginConfig.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _fail_validation(submission: Submission, reason: str) -> None:
    """Record a clean VALIDATION_FAILED with a teacher-facing reason.

    Pre-sandbox failures (no plugin config, misconfigured plan, bad/unsafe ZIP)
    are detected while the submission is still PENDING. The state machine only
    allows ``validation_failed`` from VALIDATING, so step through
    ``start_validation`` first; otherwise the transition raises and the outbox
    message loops in ERROR/retry instead of converging on VALIDATION_FAILED.
    """
    submission.test_results = {"check_reason": reason}
    if submission.status == SubmissionStatus.PENDING:
        transition(submission, "start_validation")
    transition(submission, "validation_failed")


def _accept_without_checks(submission: Submission, review_mode: str) -> None:
    """Accept a quiz-examined submission without running any check.

    No sandbox, no check plan — but the archive is still opened and safe-extracted to a
    throwaway directory, so a corrupt ZIP or one with traversal/zip-bomb entries fails here
    exactly as it would on the sandbox path rather than reaching the student's quiz.

    ``test_results`` deliberately carries no ``score``/``max_score``: ``grading._pct`` then
    returns None for the works component and ``compute_grade`` renormalises onto the quiz.
    """
    saved_as = (submission.source_metadata or {}).get("saved_as")
    if not saved_as:
        raise RuntimeError("Submission has no saved_as in source_metadata")

    with tempfile.TemporaryDirectory(prefix="submission_") as extract_dir:
        try:
            with zipfile.ZipFile(UPLOADS_DIR / saved_as, "r") as zf:
                safe_extract(zf, Path(extract_dir))
        except (zipfile.BadZipFile, OSError) as exc:
            _fail_validation(submission, f"Could not open submitted ZIP: {exc}")
            return
        except UnsafeArchiveError as exc:
            logger.error(
                "check_task_unsafe_archive", submission_id=submission.id, error=str(exc)
            )
            _fail_validation(submission, f"Submitted ZIP archive is unsafe: {exc}")
            return

    submission.test_results = {"skipped": True, "reason": review_mode}
    transition(submission, "start_validation")
    transition(submission, "validation_passed")
    transition(submission, "test_passed_quiz")
    logger.info(
        "check_task_skipped_for_quiz", submission_id=submission.id, review_mode=review_mode
    )


async def _advance_after_tests(db: AsyncSession, submission: Submission, review_mode: str) -> None:
    if review_mode == "tests_then_ai":
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(
            event_type=OutboxEventType.RUN_AI_REVIEW,
            state=OutboxMessageState.PENDING,
            payload={"submission_id": submission.id},
        ))
    elif review_mode == "tests_then_teacher":
        transition(submission, "test_passed_teacher")
        await enqueue_teacher_review_notification(db, submission.id)
    elif review_mode in ("tests_then_ai_then_teacher", "tests_then_ai_teacher"):
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(
            event_type=OutboxEventType.RUN_AI_REVIEW,
            state=OutboxMessageState.PENDING,
            payload={"submission_id": submission.id, "next_step": "teacher"},
        ))
    elif review_mode == "tests_then_ai_then_quiz":
        transition(submission, "test_passed_ai")
        db.add(OutboxMessage(
            event_type=OutboxEventType.RUN_AI_REVIEW,
            state=OutboxMessageState.PENDING,
            payload={"submission_id": submission.id, "next_step": "quiz"},
        ))
    elif review_mode == "tests_then_quiz":
        transition(submission, "test_passed_quiz")
    else:
        transition(submission, "test_passed_tests_only")
        # Tests-only submissions complete here — compute their final grade now.
        await finalize_grade(db, submission)
        sa = submission.students_assignment
        subjects_assignment = sa.subjects_assignment
        await _notify_student(
            db, sa.student_id,
            title=f"{subjects_assignment.title}: passed",
            body=f"Your submission for \"{subjects_assignment.title}\" passed the automated checks.",
            link=f"/portal/subjects/{subjects_assignment.subject_id}/assignments/{subjects_assignment.id}",
        )
