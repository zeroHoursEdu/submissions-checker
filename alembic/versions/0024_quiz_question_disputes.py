"""Broken-question disputes: a student's report and the credit a teacher grants for it.

Two tables. ``quiz_question_disputes`` is one student's complaint about one attempt;
``quiz_question_overrides`` is the teacher's ruling about a question in a plugin config
version, which outlives that attempt and reaches every student who drew the question —
including attempts still in progress, which read it when they finalize.

Purely additive; no existing row needs a backfill.

``downgrade`` drops both tables and the new enum but deliberately LEAVES the
``QUIZ_DISPUTE_RESOLVED`` value on ``outbox_event_type``: PostgreSQL has no
``ALTER TYPE ... DROP VALUE``, and 0006/0013 already established that swapping a whole
enum type is not worth it (see the retired-event comment in ``outbox_processor``).

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE quiz_dispute_status AS ENUM ('OPEN', 'ACCEPTED', 'REJECTED')")
    op.execute(
        "ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'QUIZ_DISPUTE_RESOLVED'"
    )

    op.create_table(
        "quiz_question_disputes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("student_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_config_id", sa.BigInteger(), nullable=True),
        sa.Column("plugin_config_version", sa.Integer(), nullable=True),
        sa.Column("student_note", sa.Text(), nullable=True),
        sa.Column(
            "status",
            # postgresql.ENUM, not sa.Enum: `create_type` is a dialect-level flag, and a
            # generic sa.Enum would re-emit the CREATE TYPE above and fail on the duplicate.
            postgresql.ENUM(
                "OPEN", "ACCEPTED", "REJECTED", name="quiz_dispute_status", create_type=False
            ),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("teacher_note", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["quiz_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plugin_config_id"], ["subject_plugin_configs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quiz_question_disputes_attempt_id", "quiz_question_disputes", ["attempt_id"]
    )
    op.create_index(
        "ix_quiz_question_disputes_status", "quiz_question_disputes", ["status", "created_at"]
    )
    op.create_index(
        "ix_quiz_question_disputes_scope",
        "quiz_question_disputes",
        ["plugin_config_id", "plugin_config_version", "question_id"],
    )

    op.create_table(
        "quiz_question_overrides",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plugin_config_id", sa.BigInteger(), nullable=False),
        sa.Column("plugin_config_version", sa.Integer(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("credit_all", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("dispute_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plugin_config_id"], ["subject_plugin_configs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"], ["quiz_question_disputes.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plugin_config_id",
            "plugin_config_version",
            "question_id",
            name="uq_quiz_question_override",
        ),
    )


def downgrade() -> None:
    op.drop_table("quiz_question_overrides")
    op.drop_index("ix_quiz_question_disputes_scope", table_name="quiz_question_disputes")
    op.drop_index("ix_quiz_question_disputes_status", table_name="quiz_question_disputes")
    op.drop_index("ix_quiz_question_disputes_attempt_id", table_name="quiz_question_disputes")
    op.drop_table("quiz_question_disputes")
    op.execute("DROP TYPE quiz_dispute_status")
