"""Add quiz_attempt_snapshots table and students.recording_consent_at for camera proctoring.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "students",
        sa.Column("recording_consent_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "quiz_attempt_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("s3_key", sa.String(length=512), nullable=False),
        sa.Column("s3_url", sa.String(length=1024), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quiz_attempt_snapshots_attempt_id",
        "quiz_attempt_snapshots",
        ["attempt_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quiz_attempt_snapshots_attempt_id", table_name="quiz_attempt_snapshots")
    op.drop_table("quiz_attempt_snapshots")
    op.drop_column("students", "recording_consent_at")
