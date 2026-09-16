"""Add subject_gradebook_stats cache table.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "subject_gradebook_stats",
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pending_review_count", sa.Integer(), nullable=False),
        sa.Column("average_mark_pct", sa.Float(), nullable=True),
        sa.Column("pass_pct", sa.Float(), nullable=False),
        sa.Column("cheating_pct", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subject_gradebook_stats")
