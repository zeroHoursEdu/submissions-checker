"""Google Classroom ingest and LLM grading drafts.

Purely additive — new tables and nullable columns — so a replica still on the previous
release keeps working until it is replaced.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "google_connections",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("google_email", sa.String(255), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_ts(),
    )

    op.add_column("subjects", sa.Column("classroom_course_id", sa.String(64), nullable=True))
    op.add_column("subjects", sa.Column("classroom_course_name", sa.String(255), nullable=True))
    op.add_column(
        "subjects",
        sa.Column(
            "classroom_connection_id",
            sa.BigInteger(),
            sa.ForeignKey("google_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "subjects", sa.Column("classroom_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("subjects", sa.Column("classroom_sync_error", sa.Text(), nullable=True))
    op.add_column(
        "subjects_assignments", sa.Column("classroom_coursework_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "subjects_assignments",
        sa.Column("classroom_coursework_title", sa.String(255), nullable=True),
    )

    op.create_table(
        "classroom_student_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("classroom_user_id", sa.String(64), nullable=False),
        sa.Column("classroom_email", sa.String(255), nullable=True),
        sa.Column("classroom_name", sa.String(255), nullable=False),
        sa.Column(
            "student_id",
            sa.BigInteger(),
            sa.ForeignKey("students.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("candidates", postgresql.JSONB(), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default="false"),
        *_ts(),
        sa.UniqueConstraint(
            "subject_id", "classroom_user_id", name="uq_classroom_links_subject_user"
        ),
    )

    op.create_table(
        "classroom_works",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "subjects_assignment_id",
            sa.BigInteger(),
            sa.ForeignKey("subjects_assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "link_id",
            sa.BigInteger(),
            sa.ForeignKey("classroom_student_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("classroom_submission_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        *_ts(),
        sa.UniqueConstraint(
            "classroom_submission_id", "content_hash", name="uq_classroom_works_sub_hash"
        ),
    )
    op.create_index(
        "ix_classroom_works_assignment_link",
        "classroom_works",
        ["subjects_assignment_id", "link_id"],
    )

    op.create_table(
        "llm_gradings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "classroom_work_id",
            sa.BigInteger(),
            sa.ForeignKey("classroom_works.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draft", postgresql.JSONB(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_llm_gradings_status", "llm_gradings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_llm_gradings_status", table_name="llm_gradings")
    op.drop_table("llm_gradings")
    op.drop_index("ix_classroom_works_assignment_link", table_name="classroom_works")
    op.drop_table("classroom_works")
    op.drop_table("classroom_student_links")
    op.drop_column("subjects_assignments", "classroom_coursework_title")
    op.drop_column("subjects_assignments", "classroom_coursework_id")
    op.drop_column("subjects", "classroom_sync_error")
    op.drop_column("subjects", "classroom_synced_at")
    op.drop_column("subjects", "classroom_connection_id")
    op.drop_column("subjects", "classroom_course_name")
    op.drop_column("subjects", "classroom_course_id")
    op.drop_table("google_connections")
