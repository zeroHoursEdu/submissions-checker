"""Pull and test execution tasks."""

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from submissions_checker.core.config import get_settings
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models import (
    OutboxMessage,
    OutboxEventType,
    Submission,
    SubmissionStatus,
)

logger = get_logger(__name__)


async def execute_pull_task(db: AsyncSession, pull_data: dict) -> None:
    """
    Clone fork repository for evaluation and create REVIEW task.

    This task extracts the fork repository information from the webhook payload,
    clones it to the configured workspace directory, creates or updates a Submission
    record, and creates a REVIEW outbox message for the next processing stage.

    All operations are performed within the provided database session transaction,
    ensuring atomicity with the PULL message state change.

    Expected payload structure:
    {
        "pr_number": int,
        "fork_clone_url": str,        # HTTPS clone URL of the fork
        "fork_full_name": str,        # e.g., "raiseAndCall/basics_of_python"
        "head_ref": str,              # Branch name (e.g., "main")
        "head_sha": str,              # Commit SHA
        "base_full_name": str,        # Parent repo (e.g., "javaAndScriptDeveloper/basics_of_python")
        "action": str,                # "opened" or "synchronize"
    }

    Args:
        db: Database session for transactional operations
        pull_data: Pull request and repository data from webhook payload

    Raises:
        ValueError: If required fields are missing from payload
        RuntimeError: If git clone fails
    """
    # Extract required fields
    pr_number = pull_data.get("pr_number")
    fork_clone_url = pull_data.get("fork_clone_url")
    fork_full_name = pull_data.get("fork_full_name")
    head_ref = pull_data.get("head_ref")
    head_sha = pull_data.get("head_sha")
    base_full_name = pull_data.get("base_full_name")

    logger.info(
        "execute_pull_task_started",
        pr_number=pr_number,
        fork_repo=fork_full_name,
        parent_repo=base_full_name,
        branch=head_ref,
        commit=head_sha,
    )

    # Validate required fields
    if not all([fork_clone_url, fork_full_name, head_ref, head_sha]):
        missing = [
            field for field, value in [
                ("fork_clone_url", fork_clone_url),
                ("fork_full_name", fork_full_name),
                ("head_ref", head_ref),
                ("head_sha", head_sha),
            ] if not value
        ]
        logger.error("execute_pull_task_missing_fields", missing_fields=missing)
        raise ValueError(f"Missing required fields in payload: {missing}")

    # Security (belt-and-suspenders with the webhook check): only clone trusted
    # GitHub https URLs. Reject anything else before it reaches git.
    if not isinstance(fork_clone_url, str) or not fork_clone_url.startswith(
        "https://github.com/"
    ):
        logger.error(
            "execute_pull_task_invalid_clone_url",
            pr_number=pr_number,
            fork_repo=fork_full_name,
            clone_url=fork_clone_url,
        )
        raise ValueError(f"Refusing to clone untrusted URL: {fork_clone_url}")

    try:
        # Get workspace directory from settings
        settings = get_settings()
        workspace_base = Path(settings.workspace_dir)

        # Create directory for this submission (simplified path structure without PR number)
        # Using format: workspace_dir/fork_owner/fork_repo
        fork_owner, fork_repo = fork_full_name.split("/")
        clone_path = workspace_base / fork_owner / fork_repo

        # Remove existing directory if it exists (for PR updates)
        if clone_path.exists():
            logger.info("removing_existing_clone", path=str(clone_path))
            shutil.rmtree(clone_path)

        # Clone the fork repository
        from submissions_checker.utils.git import clone_repository

        await clone_repository(
            repo_url=fork_clone_url,
            target_dir=clone_path,
            branch=head_ref,
            depth=1,  # Shallow clone for efficiency
        )

        logger.info(
            "repository_cloned",
            fork_repo=fork_full_name,
            clone_path=str(clone_path),
            commit=head_sha,
        )

        # Find or create submission record
        result = await db.execute(
            select(Submission).where(
                Submission.pr_number == pr_number,
                Submission.fork_full_name == fork_full_name,
            )
        )
        submission = result.scalar_one_or_none()

        if submission is None:
            # Create new submission
            submission = Submission(
                pr_number=pr_number,
                fork_full_name=fork_full_name,
                base_full_name=base_full_name,
                head_ref=head_ref,
                head_sha=head_sha,
                github_username=fork_owner,
                repository_path=str(clone_path),
                status=SubmissionStatus.CLONING,
            )
            db.add(submission)
            logger.info(
                "submission_created",
                pr_number=pr_number,
                fork_repo=fork_full_name,
            )
        else:
            # Update existing submission for PR updates
            submission.head_ref = head_ref
            submission.head_sha = head_sha
            submission.repository_path = str(clone_path)
            submission.status = SubmissionStatus.CLONING
            logger.info(
                "submission_updated",
                submission_id=submission.id,
                pr_number=pr_number,
            )

        # Flush to get submission.id without committing
        await db.flush()

        # Create REVIEW outbox message for next processing stage
        review_message = OutboxMessage(
            event_type=OutboxEventType.REVIEW,
            payload={"submission_id": submission.id},
        )
        db.add(review_message)

        logger.info(
            "execute_pull_task_completed",
            submission_id=submission.id,
            fork_repo=fork_full_name,
            clone_path=str(clone_path),
            review_message_created=True,
        )

    except Exception as e:
        logger.error(
            "execute_pull_task_failed",
            error=str(e),
            fork_repo=fork_full_name,
            pr_number=pr_number,
        )
        raise
