"""Add entity_type to groups and students; add subject_test_students table; seed __TEST__ group.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE entity_type AS ENUM ('REAL', 'TEST')")

    op.add_column(
        "groups",
        sa.Column(
            "type",
            sa.Enum("REAL", "TEST", name="entity_type", create_type=False),
            nullable=False,
            server_default="REAL",
        ),
    )

    op.add_column(
        "students",
        sa.Column(
            "type",
            sa.Enum("REAL", "TEST", name="entity_type", create_type=False),
            nullable=False,
            server_default="REAL",
        ),
    )

    op.create_table(
        "subject_test_students",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "student_id",
            sa.BigInteger(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plain_password", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_subject_test_students_student_id", "subject_test_students", ["student_id"])

    op.execute(
        """
        INSERT INTO groups (name, description, type, created_at, updated_at)
        VALUES ('__TEST__', 'System group for test students', 'TEST', now(), now())
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM groups WHERE name = '__TEST__'")
    op.drop_index("ix_subject_test_students_student_id", table_name="subject_test_students")
    op.drop_table("subject_test_students")
    op.drop_column("students", "type")
    op.drop_column("groups", "type")
    op.execute("DROP TYPE entity_type")
