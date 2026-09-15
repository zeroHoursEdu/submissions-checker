"""Air-raid pause: frozen-clock state on an attempt, plus a record of each pause taken.

Purely additive. ``paused_at IS NULL`` and ``paused_seconds = 0`` correctly describe every
existing attempt — none was ever paused — so no backfill is needed.

Separate from 0024 so either feature can be reverted without the other.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "quiz_attempts",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quiz_attempts",
        sa.Column("paused_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_quiz_attempts_paused_seconds", "quiz_attempts", "paused_seconds >= 0"
    )

    op.create_table(
        "quiz_attempt_pauses",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("region_uid", sa.Integer(), nullable=True),
        sa.Column("region_title", sa.String(128), nullable=True),
        sa.Column("alert_started_at", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_attempt_pauses_attempt_id", "quiz_attempt_pauses", ["attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_quiz_attempt_pauses_attempt_id", table_name="quiz_attempt_pauses")
    op.drop_table("quiz_attempt_pauses")
    op.drop_constraint("ck_quiz_attempts_paused_seconds", "quiz_attempts", type_="check")
    op.drop_column("quiz_attempts", "paused_seconds")
    op.drop_column("quiz_attempts", "paused_at")
