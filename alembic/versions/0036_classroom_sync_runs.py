"""Progress rows for manual Classroom syncs run in the background.

Purely additive: a replica still on the previous release never reads the table.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "classroom_sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String(32), nullable=True),
        sa.Column("assignments_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assignments_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submissions_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submissions_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_versions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_classroom_sync_runs_subject_id", "classroom_sync_runs", ["subject_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_classroom_sync_runs_subject_id", table_name="classroom_sync_runs")
    op.drop_table("classroom_sync_runs")
