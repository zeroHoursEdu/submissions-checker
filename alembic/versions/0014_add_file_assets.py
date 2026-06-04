"""Add file assets: subject pictures and assignment content files.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("subjects", sa.Column("grid_picture_url", sa.String(1000), nullable=True))
    op.add_column("subjects", sa.Column("main_picture_url", sa.String(1000), nullable=True))
    op.add_column(
        "subjects_assignments",
        sa.Column("content_files", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subjects_assignments", "content_files")
    op.drop_column("subjects", "main_picture_url")
    op.drop_column("subjects", "grid_picture_url")
