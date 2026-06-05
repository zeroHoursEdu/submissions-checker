"""Add ADMIN role, SHORT_ANSWER quiz type, new outbox events, password reset tokens,
audit logs, notifications table.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Enum: UserRole — add ADMIN ────────────────────────────────────────────
    # ALTER TYPE ADD VALUE cannot be used in the same transaction as the new value.
    # Commit, add enum values (auto-committed), then start a fresh transaction.
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    conn.execute(sa.text("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'ADMIN'"))
    conn.execute(sa.text("ALTER TYPE quiz_question_type ADD VALUE IF NOT EXISTS 'SHORT_ANSWER'"))
    conn.execute(sa.text("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'SUBMISSION_REVIEWED'"))
    conn.execute(sa.text("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'QUIZ_RESULT'"))
    conn.execute(sa.text("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'DEADLINE_REMINDER'"))
    conn.execute(sa.text("ALTER TYPE outbox_event_type ADD VALUE IF NOT EXISTS 'NEW_SUBMISSION'"))
    conn.execute(sa.text("BEGIN"))

    # ── Update users check constraint to allow ADMIN ──────────────────────────
    op.drop_constraint("ck_users_student_role_has_student_id", "users", type_="check")
    op.create_check_constraint(
        "ck_users_student_role_has_student_id",
        "users",
        "(role = 'STUDENT' AND student_id IS NOT NULL) OR role IN ('TEACHER', 'ADMIN')",
    )

    # ── Table: password_reset_tokens ──────────────────────────────────────────
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_password_reset_tokens_token", "password_reset_tokens", ["token"])
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    # ── Table: audit_logs ─────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_username", sa.String(100), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=True),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # ── Table: notifications ──────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "is_read"])


def downgrade() -> None:
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_token", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")

    # Restore original check constraint
    op.drop_constraint("ck_users_student_role_has_student_id", "users", type_="check")
    op.create_check_constraint(
        "ck_users_student_role_has_student_id",
        "users",
        "(role = 'STUDENT' AND student_id IS NOT NULL) OR role = 'TEACHER'",
    )
    # PostgreSQL does not support removing enum values; enum changes are left in place on downgrade
