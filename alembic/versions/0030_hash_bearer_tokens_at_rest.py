"""Store password-reset and feedback tokens hashed.

Revision ID: 0030
Revises: 0029

The raw token is the bearer secret: whoever reads the row can reset that user's
password or answer as that student. New rows store SHA-256(token) in token_hash and
leave token NULL; rows written before this revision keep their plaintext token and
are still honoured, so no link already sent stops working. Additive only: a new
nullable column plus dropping NOT NULL on the old one. Nothing is rewritten.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "password_reset_tokens", sa.Column("token_hash", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True
    )
    op.alter_column("password_reset_tokens", "token", nullable=True)

    op.add_column("feedback_tokens", sa.Column("token_hash", sa.String(64), nullable=True))
    op.create_index(
        "ix_feedback_tokens_token_hash", "feedback_tokens", ["token_hash"], unique=True
    )
    op.alter_column("feedback_tokens", "token", nullable=True)


def downgrade() -> None:
    # Rows created after the upgrade have no plaintext token; restoring NOT NULL
    # would fail on them, so the columns stay nullable on the way down.
    op.drop_index("ix_feedback_tokens_token_hash", table_name="feedback_tokens")
    op.drop_column("feedback_tokens", "token_hash")
    op.drop_index("ix_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_column("password_reset_tokens", "token_hash")
