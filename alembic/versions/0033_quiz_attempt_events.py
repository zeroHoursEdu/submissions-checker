"""Anti-cheat event timeline: one row per /event call, kept for the life of the attempt.

Purely additive (new table + enum type), so a replica still on the previous release keeps
working against the migrated schema.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels = None
depends_on = None

_OUTCOMES = (
    "APPLIED",
    "INFORMATIONAL",
    "IGNORED_PAUSED",
    "IGNORED_NOT_IN_PROGRESS",
    "IGNORED_TYPE_CAP",
)


def upgrade() -> None:
    op.execute(
        "CREATE TYPE quiz_event_outcome AS ENUM ("
        + ", ".join(f"'{v}'" for v in _OUTCOMES)
        + ")"
    )
    op.create_table(
        "quiz_attempt_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("count_after", sa.Integer(), nullable=True),
        sa.Column("rule_threshold", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(16), nullable=False, server_default="none"),
        sa.Column(
            "outcome",
            postgresql.ENUM(*_OUTCOMES, name="quiz_event_outcome", create_type=False),
            nullable=False,
        ),
        sa.Column("client_ctx", postgresql.JSONB(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_attempt_events_attempt_id", "quiz_attempt_events", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_quiz_attempt_events_attempt_id", table_name="quiz_attempt_events")
    op.drop_table("quiz_attempt_events")
    op.execute("DROP TYPE quiz_event_outcome")
