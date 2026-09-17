"""Squads: students who hand in one submission together and share its grade.

Three new tables (squads, squad_members, squad_invites), one nullable column on
subjects / submissions / quiz_attempts each. Purely additive; existing rows keep
NULL, which every reader treats as "solo".

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE squad_invite_status AS ENUM ('PENDING', 'ACCEPTED', 'DECLINED', 'CANCELLED')"
    )

    op.add_column("subjects", sa.Column("squad_max_size", sa.Integer(), nullable=True))

    op.create_table(
        "squads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("created_by_student_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_student_id"], ["students.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_squads_subject_id", "squads", ["subject_id"])

    op.create_table(
        "squad_members",
        sa.Column("squad_id", sa.BigInteger(), nullable=False),
        sa.Column("student_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["squad_id"], ["squads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("squad_id", "student_id"),
        sa.UniqueConstraint("subject_id", "student_id", name="uq_squad_members_subject_student"),
    )
    op.create_index("ix_squad_members_student_id", "squad_members", ["student_id"])

    op.create_table(
        "squad_invites",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("squad_id", sa.BigInteger(), nullable=False),
        sa.Column("invited_student_id", sa.BigInteger(), nullable=False),
        sa.Column("invited_by_student_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "ACCEPTED",
                "DECLINED",
                "CANCELLED",
                name="squad_invite_status",
                create_type=False,
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["squad_id"], ["squads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_student_id"], ["students.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_squad_invites_squad_id", "squad_invites", ["squad_id"])
    op.create_index(
        "ix_squad_invites_invited_student_id", "squad_invites", ["invited_student_id"]
    )
    op.create_index(
        "uq_squad_invites_pending",
        "squad_invites",
        ["squad_id", "invited_student_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.add_column("submissions", sa.Column("squad_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_submissions_squad_id", "submissions", "squads", ["squad_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_submissions_squad_id", "submissions", ["squad_id"])

    op.add_column("quiz_attempts", sa.Column("student_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_quiz_attempts_student_id",
        "quiz_attempts",
        "students",
        ["student_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_quiz_attempts_student_id", "quiz_attempts", ["student_id"])


def downgrade() -> None:
    op.drop_index("ix_quiz_attempts_student_id", table_name="quiz_attempts")
    op.drop_constraint("fk_quiz_attempts_student_id", "quiz_attempts", type_="foreignkey")
    op.drop_column("quiz_attempts", "student_id")
    op.drop_index("ix_submissions_squad_id", table_name="submissions")
    op.drop_constraint("fk_submissions_squad_id", "submissions", type_="foreignkey")
    op.drop_column("submissions", "squad_id")
    op.drop_table("squad_invites")
    op.drop_table("squad_members")
    op.drop_table("squads")
    op.drop_column("subjects", "squad_max_size")
    op.execute("DROP TYPE squad_invite_status")
