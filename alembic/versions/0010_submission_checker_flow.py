"""Add CHECKING, CHECK_FAILED, WAITING_FOR_TEACHER_REVIEW submission statuses.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE submission_status ADD VALUE 'CHECKING'")
    op.execute("ALTER TYPE submission_status ADD VALUE 'CHECK_FAILED'")
    op.execute("ALTER TYPE submission_status ADD VALUE 'WAITING_FOR_TEACHER_REVIEW'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade leaves them in place
    pass
