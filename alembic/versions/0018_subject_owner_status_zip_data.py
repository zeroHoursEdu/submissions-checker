"""Add subject owner_id, status, partial unique index on code; add zip_data to subject_plugin_configs.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Create the subject_status enum type in PostgreSQL
    op.execute("CREATE TYPE subject_status AS ENUM ('ACTIVE', 'DELETED')")

    # Drop existing unique constraint on subjects.code (replaced by partial index below)
    op.drop_constraint("uq_subjects_code", "subjects", type_="unique")

    # Add owner_id column (nullable — legacy rows created by startup loader have no owner)
    op.add_column(
        "subjects",
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Add status column with ACTIVE default so existing rows are unaffected
    op.add_column(
        "subjects",
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "DELETED", name="subject_status", create_type=False),
            nullable=False,
            server_default="ACTIVE",
        ),
    )

    # Partial unique index: at most one ACTIVE subject per code
    op.create_index(
        "uix_subjects_active_code",
        "subjects",
        ["code"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index("ix_subjects_status", "subjects", ["status"])
    op.create_index("ix_subjects_owner_id", "subjects", ["owner_id"])

    # Add zip_data column to subject_plugin_configs (nullable — startup-loaded rows leave it NULL)
    op.add_column(
        "subject_plugin_configs",
        sa.Column("zip_data", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subject_plugin_configs", "zip_data")

    op.drop_index("ix_subjects_owner_id", table_name="subjects")
    op.drop_index("ix_subjects_status", table_name="subjects")
    op.drop_index("uix_subjects_active_code", table_name="subjects")

    op.drop_column("subjects", "status")
    op.drop_column("subjects", "owner_id")

    # Restore original unique constraint on subjects.code
    op.create_unique_constraint("uq_subjects_code", "subjects", ["code"])

    op.execute("DROP TYPE subject_status")
