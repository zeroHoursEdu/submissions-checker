"""Worker task definitions."""

from submissions_checker.workers.tasks.review_tasks import execute_ai_review_task

__all__ = [
    "execute_ai_review_task",
]
