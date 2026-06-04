"""Add student registration support: SEND_CREDENTIALS event type, nullable github_username, user_logins table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-30

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Add SEND_CREDENTIALS to outbox_event_type enum
    conn.execute(text("ALTER TYPE outbox_event_type ADD VALUE 'SEND_CREDENTIALS'"))

    # Make students.github_username nullable (CSV registration doesn't require it)
    conn.execute(text("ALTER TABLE students ALTER COLUMN github_username DROP NOT NULL"))

    # Create user_logins table for first-login and activity tracking
    conn.execute(
        text("""
        CREATE TABLE user_logins (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            logged_in_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """)
    )

    conn.execute(text("CREATE INDEX ix_user_logins_user_id ON user_logins(user_id)"))
    conn.execute(text("CREATE INDEX ix_user_logins_logged_in_at ON user_logins(logged_in_at)"))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(text("DROP TABLE user_logins"))

    # Restore NOT NULL on github_username (only safe if no NULLs exist)
    conn.execute(text("ALTER TABLE students ALTER COLUMN github_username SET NOT NULL"))

    # PostgreSQL does not support removing enum values; downgrade leaves SEND_CREDENTIALS in place
