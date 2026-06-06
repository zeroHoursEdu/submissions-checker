"""Add feedback_requests, feedback_tokens, feedback_responses tables.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'FEEDBACK_REQUEST_SENT'")
    op.create_table(
        "feedback_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("semester_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_teacher_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["semester_id"], ["semesters.id"]),
        sa.ForeignKeyConstraint(["created_by_teacher_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_id", "semester_id", name="uq_feedback_request_subject_semester"),
    )
    op.create_index("ix_feedback_requests_subject_id", "feedback_requests", ["subject_id"])

    op.create_table(
        "feedback_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feedback_request_id", sa.BigInteger(), nullable=False),
        sa.Column("student_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["feedback_request_id"], ["feedback_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_feedback_tokens_token"),
    )
    op.create_index("ix_feedback_tokens_feedback_request_id", "feedback_tokens", ["feedback_request_id"])

    op.create_table(
        "feedback_responses",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feedback_token_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("went_well", sa.Text(), nullable=False),
        sa.Column("went_bad", sa.Text(), nullable=False),
        sa.Column("to_change", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["feedback_token_id"], ["feedback_tokens.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feedback_token_id", name="uq_feedback_response_token"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_feedback_response_rating"),
    )
    op.create_index("ix_feedback_responses_subject_id", "feedback_responses", ["subject_id"])


def downgrade() -> None:
    op.drop_index("ix_feedback_responses_subject_id", table_name="feedback_responses")
    op.drop_table("feedback_responses")
    op.drop_index("ix_feedback_tokens_feedback_request_id", table_name="feedback_tokens")
    op.drop_table("feedback_tokens")
    op.drop_index("ix_feedback_requests_subject_id", table_name="feedback_requests")
    op.drop_table("feedback_requests")
