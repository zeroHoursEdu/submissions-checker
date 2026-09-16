"""Retire legacy submission statuses, GitHub source types and outbox events.

Revision ID: 0027
Revises: 0026

The GitHub-PR ingest and the pre-2026-06 "CHECKING" flow are gone from the code.
Historical rows are mapped to their closest current value before the enum
values are removed, so this is safe on any database regardless of contents.
PostgreSQL cannot drop an enum value, hence the rename/recreate dance.
"""

from __future__ import annotations

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None

_STATUS_MAP = {
    "PROCESSING": "VALIDATING",
    "CHECKING": "TESTING",
    "REVIEWING": "AWAITING_AI_REVIEW",
    "CHECK_FAILED": "TEST_FAILED",
    "WAITING_FOR_TEACHER_REVIEW": "AWAITING_TEACHER_REVIEW",
}
_STATUS_VALUES = (
    "PENDING",
    "VALIDATING",
    "VALIDATION_FAILED",
    "TESTING",
    "TEST_FAILED",
    "AWAITING_AI_REVIEW",
    "AI_REVIEWING",
    "AI_REVIEW_FAILED",
    "AWAITING_TEACHER_REVIEW",
    "QUIZ_SENT",
    "COMPLETED",
    "FAILED",
)
_EVENT_VALUES = (
    "SEND_CREDENTIALS",
    "SUBMISSION_REVIEWED",
    "QUIZ_RESULT",
    "DEADLINE_REMINDER",
    "NEW_SUBMISSION",
    "RUN_CHECKS",
    "RUN_AI_REVIEW",
    "FEEDBACK_REQUEST_SENT",
    "QUIZ_DISPUTE_RESOLVED",
)


def _recreate_enum(type_name: str, table: str, column: str, values: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_name} "
        f"USING {column}::text::{type_name}"
    )
    op.execute(f"DROP TYPE {type_name}_old")


def upgrade() -> None:
    # 1. submissions.status — map legacy values, then recreate the enum.
    for old, new in _STATUS_MAP.items():
        op.execute(f"UPDATE submissions SET status = '{new}' WHERE status = '{old}'")
    op.execute("ALTER TABLE submissions ALTER COLUMN status DROP DEFAULT")
    _recreate_enum("submission_status", "submissions", "status", _STATUS_VALUES)
    op.execute("ALTER TABLE submissions ALTER COLUMN status SET DEFAULT 'PENDING'")

    # 2. submissions.source_type — everything is a ZIP upload now.
    op.execute(
        "UPDATE submissions SET source_type = 'ZIP_UPLOAD' WHERE source_type <> 'ZIP_UPLOAD'"
    )
    _recreate_enum("submission_source_type", "submissions", "source_type", ("ZIP_UPLOAD",))

    # 3. outbox_messages.event_type — stray retired rows carry no work; delete them.
    op.execute("DELETE FROM outbox_messages WHERE event_type IN ('PULL', 'REVIEW', 'NOTIFY')")
    _recreate_enum("outbox_event_type", "outbox_messages", "event_type", _EVENT_VALUES)

    # 4. GitHub-era columns.
    op.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS uq_students_github_username")
    op.drop_column("students", "github_username")
    op.drop_column("subjects", "github_repo")


def downgrade() -> None:
    raise RuntimeError("0027 is irreversible: legacy enum values were mapped away")
