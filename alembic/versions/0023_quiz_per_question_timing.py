"""Per-question quiz timing: attempt cursor/clock and a timed-out flag on answers.

Purely additive. Existing attempts are either terminal or single-page, and the column
defaults (current_index=0, question_started_at=NULL, timed_out=false) already describe
them correctly, so no backfill is needed.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "quiz_attempts",
        sa.Column("current_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "quiz_attempts",
        sa.Column("question_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "quiz_answers",
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("quiz_answers", "timed_out")
    op.drop_column("quiz_attempts", "question_started_at")
    op.drop_column("quiz_attempts", "current_index")
