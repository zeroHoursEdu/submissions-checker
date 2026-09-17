"""Retire legacy submission statuses, GitHub source types and outbox events.

Revision ID: 0027
Revises: 0026

The GitHub-PR ingest and the pre-2026-06 "CHECKING" flow are gone from the code.
Historical rows are mapped to their closest current value before the enum
values are removed, so this is safe on any database regardless of contents.
PostgreSQL cannot drop an enum value, hence the rename/recreate dance. The enum
swap is compatible with the previous release running alongside (it never wrote
the removed values); the GitHub-era columns are deliberately left in place for
the same reason.
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


def _recreate_enum(
    type_name: str,
    table: str,
    column: str,
    values: tuple[str, ...],
    mapping: dict[str, str] | None = None,
) -> None:
    """Swap *column* onto a freshly created enum holding only *values*.

    Any legacy value listed in *mapping* is rewritten inside the USING cast. That
    matters on a fresh database: every migration runs in one transaction, and
    PostgreSQL refuses to *use* a value added with ADD VALUE (0010 and later)
    until that transaction commits — so an UPDATE naming 'VALIDATING' would fail.
    A CASE over the text form and a cast to the brand-new type sidesteps that.
    """
    quoted = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({quoted})")
    if mapping:
        whens = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
        using = f"(CASE {column}::text {whens} ELSE {column}::text END)::{type_name}"
    else:
        using = f"{column}::text::{type_name}"
    op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {type_name} USING {using}")
    op.execute(f"DROP TYPE {type_name}_old")


def upgrade() -> None:
    # 1. submissions.status — legacy values are mapped inside the type swap.
    op.execute("ALTER TABLE submissions ALTER COLUMN status DROP DEFAULT")
    _recreate_enum("submission_status", "submissions", "status", _STATUS_VALUES, _STATUS_MAP)
    op.execute("ALTER TABLE submissions ALTER COLUMN status SET DEFAULT 'PENDING'")

    # 2. submissions.source_type — everything is a ZIP upload now.
    _recreate_enum(
        "submission_source_type",
        "submissions",
        "source_type",
        ("ZIP_UPLOAD",),
        {"GITHUB_PR": "ZIP_UPLOAD", "GITLAB_MR": "ZIP_UPLOAD"},
    )

    # 3. outbox_messages.event_type — stray retired rows carry no work; delete them.
    op.execute("DELETE FROM outbox_messages WHERE event_type IN ('PULL', 'REVIEW', 'NOTIFY')")
    _recreate_enum("outbox_event_type", "outbox_messages", "event_type", _EVENT_VALUES)

    # 4. GitHub-era columns (students.github_username, subjects.github_repo) are NOT
    # dropped here. A rolling deploy runs the previous release alongside this one, and
    # that release still selects both columns; dropping them would 500 every request the
    # old replica serves until Watchtower replaces it. The ORM no longer declares them,
    # so they sit unused (nullable) until a later release drops them — see
    # docs/deployment.md, "The migration rule". Only the unique constraint goes, so the
    # new code's inserts (which leave the column NULL) can never collide.
    op.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS uq_students_github_username")


def downgrade() -> None:
    raise RuntimeError("0027 is irreversible: legacy enum values were mapped away")
