"""Remove quiz_templates and quiz_questions tables; add plugin_config tracking to quiz_attempts.

config.yml is now the single source of truth for quiz questions. The DB only tracks
quiz attempts and links each attempt back to the plugin config version it was drawn from.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Remove quiz_template_id and template_version from quiz_attempts
    op.execute("ALTER TABLE quiz_attempts DROP CONSTRAINT IF EXISTS quiz_attempts_quiz_template_id_fkey")
    op.execute("DROP INDEX IF EXISTS ix_quiz_attempts_template_id")
    op.execute("ALTER TABLE quiz_attempts DROP COLUMN IF EXISTS quiz_template_id")
    op.execute("ALTER TABLE quiz_attempts DROP COLUMN IF EXISTS template_version")

    # 2. Add plugin config tracking columns
    op.execute("""
        ALTER TABLE quiz_attempts
            ADD COLUMN plugin_config_id BIGINT
                REFERENCES subject_plugin_configs(id) ON DELETE SET NULL,
            ADD COLUMN plugin_config_version INTEGER
    """)
    op.execute(
        "CREATE INDEX ix_quiz_attempts_plugin_config_id ON quiz_attempts(plugin_config_id)"
    )

    # 3. Drop question pool tables (quiz_questions first — FK dependency on quiz_templates)
    op.execute("DROP TABLE IF EXISTS quiz_questions")
    op.execute("DROP TABLE IF EXISTS quiz_templates")


def downgrade() -> None:
    # Recreate quiz_templates and quiz_questions (data is lost — structural downgrade only)
    op.execute("""
        CREATE TABLE quiz_templates (
            id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subjects_assignment_id   BIGINT NOT NULL
                                         REFERENCES subjects_assignments(id) ON DELETE CASCADE,
            version                  INTEGER NOT NULL DEFAULT 1,
            config                   JSONB NOT NULL DEFAULT '{}',
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_quiz_templates_sa_id UNIQUE (subjects_assignment_id),
            CONSTRAINT ck_quiz_templates_version CHECK (version >= 1)
        )
    """)
    op.execute("""
        CREATE TABLE quiz_questions (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            template_id BIGINT NOT NULL REFERENCES quiz_templates(id) ON DELETE CASCADE,
            type        quiz_question_type NOT NULL,
            text        TEXT NOT NULL,
            points      INTEGER NOT NULL DEFAULT 1,
            is_required BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            config      JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_quiz_questions_points CHECK (points >= 0)
        )
    """)

    # Remove new columns
    op.execute("DROP INDEX IF EXISTS ix_quiz_attempts_plugin_config_id")
    op.execute("ALTER TABLE quiz_attempts DROP COLUMN IF EXISTS plugin_config_id")
    op.execute("ALTER TABLE quiz_attempts DROP COLUMN IF EXISTS plugin_config_version")

    # Restore old columns as nullable (cannot restore FK references after data was cleared)
    op.execute("""
        ALTER TABLE quiz_attempts
            ADD COLUMN quiz_template_id BIGINT REFERENCES quiz_templates(id) ON DELETE RESTRICT,
            ADD COLUMN template_version INTEGER
    """)
    op.execute("CREATE INDEX ix_quiz_attempts_template_id ON quiz_attempts(quiz_template_id)")
