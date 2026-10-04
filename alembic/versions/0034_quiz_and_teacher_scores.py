"""quiz_and_teacher_scores: upload-less submissions and teacher-entered points.

Purely additive — a new enum value and a nullable column — so a replica still on the
previous release keeps working until it is replaced. The enum value cannot be dropped on
downgrade (PostgreSQL has no DROP VALUE); it is harmless when unused.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE submission_source_type ADD VALUE IF NOT EXISTS 'QUIZ_ONLY'")
    op.add_column(
        "students_assignments",
        sa.Column("teacher_scores", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("students_assignments", "teacher_scores")
