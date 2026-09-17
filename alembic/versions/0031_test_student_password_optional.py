"""Stop requiring a plaintext password on subject_test_students.

Revision ID: 0031
Revises: 0030

The test-student password was stored in clear and shown on the subject page.
"Enter as test student" signs the teacher in directly, so the value has no use.
New rows leave it NULL; existing rows are not rewritten. Additive: only the
NOT NULL constraint is dropped.
"""

from __future__ import annotations

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("subject_test_students", "plain_password", nullable=True)


def downgrade() -> None:
    # Rows created after the upgrade hold NULL; NOT NULL cannot be restored on them.
    pass
