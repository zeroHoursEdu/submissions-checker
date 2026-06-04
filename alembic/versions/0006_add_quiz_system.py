"""Replace Google-Forms quiz with built-in teacher-created quiz system.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-30

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Remove old Google-Forms quiz system
    op.execute("DROP TABLE IF EXISTS quizzes CASCADE")

    # Remove GENERATE_QUIZ and NOTIFY_QUIZ_RESULT from outbox_event_type enum.
    # PostgreSQL does not support dropping enum values directly, so we:
    # 1. Delete any pending messages with the removed types
    # 2. Create a replacement enum, swap the column, drop the old type
    op.execute(
        "DELETE FROM outbox_messages WHERE event_type IN ('GENERATE_QUIZ', 'NOTIFY_QUIZ_RESULT')"
    )
    op.execute("""
        CREATE TYPE outbox_event_type_new AS ENUM (
            'PULL', 'REVIEW', 'NOTIFY', 'SEND_CREDENTIALS'
        )
    """)
    op.execute("""
        ALTER TABLE outbox_messages
            ALTER COLUMN event_type TYPE outbox_event_type_new
            USING event_type::text::outbox_event_type_new
    """)
    op.execute("DROP TYPE outbox_event_type")
    op.execute("ALTER TYPE outbox_event_type_new RENAME TO outbox_event_type")

    # New enum types
    op.execute("""
        CREATE TYPE quiz_question_type AS ENUM (
            'SINGLE_CHOICE', 'MULTIPLE_CHOICE', 'ORDERING', 'TRUE_FALSE'
        )
    """)
    op.execute("""
        CREATE TYPE quiz_attempt_status AS ENUM (
            'IN_PROGRESS', 'COMPLETED', 'TIMED_OUT'
        )
    """)

    # quiz_templates — one per assignment
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
    op.execute(
        "CREATE INDEX ix_quiz_templates_sa_id ON quiz_templates(subjects_assignment_id)"
    )
    op.execute("""
        CREATE TRIGGER trg_quiz_templates_updated_at
            BEFORE UPDATE ON quiz_templates
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # quiz_questions — questions belonging to a template
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
    op.execute("CREATE INDEX ix_quiz_questions_template_id ON quiz_questions(template_id)")
    op.execute("""
        CREATE TRIGGER trg_quiz_questions_updated_at
            BEFORE UPDATE ON quiz_questions
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # quiz_attempts — one per submission (snapshot of questions used)
    op.execute("""
        CREATE TABLE quiz_attempts (
            id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            submission_id       BIGINT NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
            quiz_template_id    BIGINT NOT NULL REFERENCES quiz_templates(id) ON DELETE RESTRICT,
            template_version    INTEGER NOT NULL,
            questions_snapshot  JSONB NOT NULL,
            config_snapshot     JSONB NOT NULL DEFAULT '{}',
            started_at          TIMESTAMPTZ NOT NULL,
            submitted_at        TIMESTAMPTZ,
            score               INTEGER,
            max_score           INTEGER,
            is_passed           BOOLEAN,
            status              quiz_attempt_status NOT NULL DEFAULT 'IN_PROGRESS',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_quiz_attempts_submission_id UNIQUE (submission_id),
            CONSTRAINT ck_quiz_attempts_score CHECK (
                score IS NULL OR (score >= 0 AND max_score IS NOT NULL AND score <= max_score)
            )
        )
    """)
    op.execute("CREATE INDEX ix_quiz_attempts_submission_id ON quiz_attempts(submission_id)")
    op.execute("CREATE INDEX ix_quiz_attempts_template_id ON quiz_attempts(quiz_template_id)")
    op.execute("CREATE INDEX ix_quiz_attempts_status ON quiz_attempts(status)")
    op.execute("""
        CREATE TRIGGER trg_quiz_attempts_updated_at
            BEFORE UPDATE ON quiz_attempts
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # quiz_answers — per-question answers for an attempt
    # question_id has no FK intentionally: question may be deleted after attempt is completed
    op.execute("""
        CREATE TABLE quiz_answers (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            attempt_id    BIGINT NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
            question_id   BIGINT NOT NULL,
            answer        JSONB NOT NULL,
            is_correct    BOOLEAN,
            points_earned INTEGER,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_quiz_answers_points_earned
                CHECK (points_earned IS NULL OR points_earned >= 0)
        )
    """)
    op.execute("CREATE INDEX ix_quiz_answers_attempt_id ON quiz_answers(attempt_id)")
    op.execute("""
        CREATE TRIGGER trg_quiz_answers_updated_at
            BEFORE UPDATE ON quiz_answers
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    for table in ["quiz_answers", "quiz_attempts", "quiz_questions", "quiz_templates"]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute("DROP TYPE IF EXISTS quiz_attempt_status")
    op.execute("DROP TYPE IF EXISTS quiz_question_type")

    # Restore outbox_event_type with old values
    op.execute("""
        CREATE TYPE outbox_event_type_old AS ENUM (
            'PULL', 'REVIEW', 'NOTIFY', 'GENERATE_QUIZ', 'NOTIFY_QUIZ_RESULT', 'SEND_CREDENTIALS'
        )
    """)
    op.execute("""
        ALTER TABLE outbox_messages
            ALTER COLUMN event_type TYPE outbox_event_type_old
            USING event_type::text::outbox_event_type_old
    """)
    op.execute("DROP TYPE outbox_event_type")
    op.execute("ALTER TYPE outbox_event_type_old RENAME TO outbox_event_type")

    # Restore quizzes table
    op.execute("""
        CREATE TABLE quizzes (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            submission_id BIGINT NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
            quiz_url      VARCHAR(1000) NOT NULL,
            score         INTEGER,
            max_score     INTEGER,
            completed_at  TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_quizzes_submission_id UNIQUE (submission_id),
            CONSTRAINT ck_quizzes_score CHECK (
                score IS NULL OR (score >= 0 AND max_score IS NOT NULL AND score <= max_score)
            )
        )
    """)
    op.execute("""
        CREATE TRIGGER trg_quizzes_updated_at
            BEFORE UPDATE ON quizzes
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)
