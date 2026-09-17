"""Record when a user's password last changed, so older sessions can be refused.

Revision ID: 0029
Revises: 0028

Additive: one nullable column. NULL means "never changed since this column
existed", and sessions issued before the column existed carry no issue time,
so nothing already logged in is affected by the migration itself.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
