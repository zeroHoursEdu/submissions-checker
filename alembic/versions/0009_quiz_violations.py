"""Add violations JSONB column to quiz_attempts and VIOLATION_FAIL status.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE quiz_attempts ADD COLUMN violations JSONB NOT NULL DEFAULT '{}'"
    )
    # PostgreSQL supports adding enum values without rewriting the table
    op.execute("ALTER TYPE quiz_attempt_status ADD VALUE 'VIOLATION_FAIL'")


def downgrade() -> None:
    op.execute("ALTER TABLE quiz_attempts DROP COLUMN IF EXISTS violations")
    # PostgreSQL does not support removing enum values; downgrade leaves VIOLATION_FAIL in place
