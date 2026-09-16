"""Quiz dummy data — retired.

Revision ID: 0007
Revises: 0006

This migration used to seed quiz_templates/quiz_questions for development.
0013 drops those tables on the same fresh-database run, so the seed never
survived. Kept as a no-op so existing databases keep a linear history.
"""

from __future__ import annotations

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
