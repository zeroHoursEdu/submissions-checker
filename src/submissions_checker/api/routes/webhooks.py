"""GitHub webhook endpoints."""

from fastapi import APIRouter, Header, Request

from submissions_checker.api.dependencies import DBSession
from submissions_checker.core.logging import get_logger
from submissions_checker.db.models.enums import OutboxEventType
from submissions_checker.db.models.outbox import OutboxMessage

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github")
async def handle_github_webhook(
    request: Request,
    db: DBSession,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
) -> dict[str, str]:
    """Handle GitHub webhook events for pull request submissions."""
    logger.info(
        "github_webhook_received",
        event_type=x_github_event,
        has_signature=x_hub_signature_256 is not None,
    )

    body = await request.body()

    if x_github_event != "pull_request":
        logger.info("github_webhook_ignored", event_type=x_github_event)
        return {"status": "ignored", "message": f"Event type '{x_github_event}' not processed"}

    payload = await request.json()
    action = payload.get("action")

    if action not in ["opened", "synchronize"]:
        logger.info("github_webhook_ignored_action", action=action)
        return {"status": "ignored", "message": f"Action '{action}' not processed"}

    pr_data = payload.get("pull_request", {})
    pr_number = pr_data.get("number")

    head = pr_data.get("head", {})
    head_repo = head.get("repo")

    if not head_repo:
        logger.error("github_webhook_missing_head_repo", pr_number=pr_number)
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Missing head repository in payload")

    fork_clone_url = head_repo.get("clone_url")
    fork_full_name = head_repo.get("full_name")
    head_ref = head.get("ref")
    head_sha = head.get("sha")

    base = pr_data.get("base", {})
    base_repo = base.get("repo", {})
    base_full_name = base_repo.get("full_name")

    logger.info(
        "processing_fork_pr",
        pr_number=pr_number,
        fork_repo=fork_full_name,
        parent_repo=base_full_name,
        branch=head_ref,
        commit=head_sha,
    )

    outbox_message = OutboxMessage(
        event_type=OutboxEventType.PULL,
        payload={
            "pr_number": pr_number,
            "fork_clone_url": fork_clone_url,
            "fork_full_name": fork_full_name,
            "head_ref": head_ref,
            "head_sha": head_sha,
            "base_full_name": base_full_name,
            "action": action,
        }
    )

    db.add(outbox_message)
    await db.commit()

    logger.info(
        "outbox_message_created",
        outbox_id=outbox_message.id,
        event_type=outbox_message.event_type.value,
    )

    return {
        "status": "accepted",
        "message": "Pull request webhook received",
        "outbox_id": str(outbox_message.id),
    }
