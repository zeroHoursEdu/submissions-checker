"""Add users.email and teacher_notification_queue for coalesced teacher digests.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(255), nullable=True))

    op.create_table(
        "teacher_notification_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "teacher_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("teacher_id", "submission_id", name="uq_teacher_notification_queue"),
    )
    op.create_index(
        "ix_teacher_notification_queue_pending",
        "teacher_notification_queue",
        ["teacher_id", "sent_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_teacher_notification_queue_pending", table_name="teacher_notification_queue"
    )
    op.drop_table("teacher_notification_queue")
    op.drop_column("users", "email")
