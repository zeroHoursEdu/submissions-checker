"""Add plugin system: subject_plugin_configs table, subject.code, subjects_assignments.code,
student_assignments.variant, submissions.plugin_config_id, new enum values.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enum: SubmissionStatus — add new precise statuses ─────────────────────
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'VALIDATING'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'VALIDATION_FAILED'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'TESTING'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'TEST_FAILED'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'AWAITING_AI_REVIEW'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'AI_REVIEWING'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'AI_REVIEW_FAILED'")
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'AWAITING_TEACHER_REVIEW'")

    # ── Enum: OutboxEventType — add new event types ───────────────────────────
    op.execute("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'RUN_CHECKS'")
    op.execute("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'RUN_AI_REVIEW'")

    # ── Table: subject_plugin_configs ─────────────────────────────────────────
    op.create_table(
        "subject_plugin_configs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger(), sa.ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("loaded_from", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_subject_plugin_configs_subject_version", "subject_plugin_configs", ["subject_id", "version"]
    )
    op.create_unique_constraint(
        "uq_subject_plugin_configs_subject_hash", "subject_plugin_configs", ["subject_id", "content_hash"]
    )
    op.create_index(
        "ix_subject_plugin_configs_subject_version", "subject_plugin_configs", ["subject_id", "version"]
    )

    # ── Table: subjects — add code column ─────────────────────────────────────
    op.add_column("subjects", sa.Column("code", sa.String(100), nullable=True))
    op.create_unique_constraint("uq_subjects_code", "subjects", ["code"])

    # ── Table: subjects_assignments — add code column ─────────────────────────
    op.add_column("subjects_assignments", sa.Column("code", sa.String(100), nullable=True))
    op.create_unique_constraint(
        "uq_subjects_assignments_subject_code", "subjects_assignments", ["subject_id", "code"]
    )

    # ── Table: students_assignments — add variant column ──────────────────────
    op.add_column("students_assignments", sa.Column("variant", sa.String(50), nullable=True))

    # ── Table: submissions — add plugin_config_id column ──────────────────────
    op.add_column(
        "submissions",
        sa.Column(
            "plugin_config_id",
            sa.BigInteger(),
            sa.ForeignKey("subject_plugin_configs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_submissions_plugin_config_id", "submissions", ["plugin_config_id"])


def downgrade() -> None:
    op.drop_index("ix_submissions_plugin_config_id", table_name="submissions")
    op.drop_column("submissions", "plugin_config_id")

    op.drop_column("students_assignments", "variant")

    op.drop_constraint("uq_subjects_assignments_subject_code", "subjects_assignments", type_="unique")
    op.drop_column("subjects_assignments", "code")

    op.drop_constraint("uq_subjects_code", "subjects", type_="unique")
    op.drop_column("subjects", "code")

    op.drop_index("ix_subject_plugin_configs_subject_version", table_name="subject_plugin_configs")
    op.drop_constraint("uq_subject_plugin_configs_subject_hash", "subject_plugin_configs", type_="unique")
    op.drop_constraint("uq_subject_plugin_configs_subject_version", "subject_plugin_configs", type_="unique")
    op.drop_table("subject_plugin_configs")
    # Note: enum values cannot be removed from PostgreSQL enums without dropping and recreating them
