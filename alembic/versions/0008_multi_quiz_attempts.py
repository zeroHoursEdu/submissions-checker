"""Allow multiple quiz attempts per submission (drop unique constraint).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE quiz_attempts DROP CONSTRAINT IF EXISTS uq_quiz_attempts_submission_id")
    # ix_quiz_attempts_submission_id already exists from 0006; keep it as a non-unique index


def downgrade() -> None:
    # Remove duplicates first (keep the oldest attempt per submission), then restore unique constraint
    op.execute("""
        DELETE FROM quiz_attempts
        WHERE id NOT IN (
            SELECT MIN(id) FROM quiz_attempts GROUP BY submission_id
        )
    """)
    op.execute(
        "ALTER TABLE quiz_attempts ADD CONSTRAINT uq_quiz_attempts_submission_id UNIQUE (submission_id)"
    )
