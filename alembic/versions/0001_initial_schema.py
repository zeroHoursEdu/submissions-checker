"""Initial schema — full normalized schema from scratch.

Revision ID: 0001
Revises:
Create Date: 2026-05-30

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create enum types (all values UPPERCASE)
    op.execute("""
        CREATE TYPE submission_status AS ENUM (
            'PENDING', 'PROCESSING', 'REVIEWING', 'QUIZ_SENT', 'COMPLETED', 'FAILED'
        )
    """)
    op.execute("""
        CREATE TYPE submission_source_type AS ENUM (
            'GITHUB_PR', 'GITLAB_MR', 'ZIP_UPLOAD'
        )
    """)
    op.execute("""
        CREATE TYPE outbox_event_type AS ENUM (
            'PULL', 'REVIEW', 'NOTIFY', 'GENERATE_QUIZ', 'NOTIFY_QUIZ_RESULT'
        )
    """)
    op.execute("""
        CREATE TYPE outbox_message_state AS ENUM (
            'PENDING', 'FINISHED', 'ERROR'
        )
    """)

    # Shared updated_at trigger function
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # groups
    op.execute("""
        CREATE TABLE groups (
            id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name    VARCHAR(255) NOT NULL,
            description TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_groups_name UNIQUE (name)
        )
    """)
    op.execute("""
        CREATE TRIGGER trg_groups_updated_at
            BEFORE UPDATE ON groups
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # students
    op.execute("""
        CREATE TABLE students (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            group_id        BIGINT NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            github_username VARCHAR(255) NOT NULL,
            email           VARCHAR(255) NOT NULL,
            full_name       VARCHAR(255) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_students_github_username UNIQUE (github_username),
            CONSTRAINT uq_students_email UNIQUE (email)
        )
    """)
    op.execute("CREATE INDEX ix_students_group_id ON students(group_id)")
    op.execute("""
        CREATE TRIGGER trg_students_updated_at
            BEFORE UPDATE ON students
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # subjects
    op.execute("""
        CREATE TABLE subjects (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            description TEXT,
            github_repo VARCHAR(500),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_subjects_name UNIQUE (name)
        )
    """)
    op.execute("""
        CREATE TRIGGER trg_subjects_updated_at
            BEFORE UPDATE ON subjects
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # subjects_students (enrollment)
    op.execute("""
        CREATE TABLE subjects_students (
            subject_id  BIGINT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            student_id  BIGINT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_subjects_students PRIMARY KEY (subject_id, student_id)
        )
    """)
    op.execute("CREATE INDEX ix_subjects_students_student_id ON subjects_students(student_id)")

    # subjects_assignments
    op.execute("""
        CREATE TABLE subjects_assignments (
            id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subject_id  BIGINT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            title       VARCHAR(255) NOT NULL,
            description TEXT,
            deadline    TIMESTAMPTZ,
            min_grade   INTEGER NOT NULL DEFAULT 0,
            max_grade   INTEGER NOT NULL DEFAULT 100,
            config      JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_subjects_assignments_grade_range
                CHECK (min_grade >= 0 AND max_grade >= min_grade)
        )
    """)
    op.execute("CREATE INDEX ix_subjects_assignments_subject_id ON subjects_assignments(subject_id)")
    op.execute("""
        CREATE TRIGGER trg_subjects_assignments_updated_at
            BEFORE UPDATE ON subjects_assignments
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # students_assignments
    op.execute("""
        CREATE TABLE students_assignments (
            id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id             BIGINT NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
            subjects_assignment_id BIGINT NOT NULL REFERENCES subjects_assignments(id) ON DELETE RESTRICT,
            grade                  INTEGER,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_students_assignments UNIQUE (student_id, subjects_assignment_id)
        )
    """)
    op.execute("CREATE INDEX ix_students_assignments_student_id ON students_assignments(student_id)")
    op.execute("""
        CREATE INDEX ix_students_assignments_subjects_assignment_id
            ON students_assignments(subjects_assignment_id)
    """)
    op.execute("""
        CREATE TRIGGER trg_students_assignments_updated_at
            BEFORE UPDATE ON students_assignments
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # submissions
    op.execute("""
        CREATE TABLE submissions (
            id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            students_assignment_id BIGINT NOT NULL REFERENCES students_assignments(id) ON DELETE RESTRICT,
            source_type            submission_source_type NOT NULL,
            source_metadata        JSONB NOT NULL DEFAULT '{}',
            repository_path        VARCHAR(500),
            status                 submission_status NOT NULL DEFAULT 'PENDING',
            test_results           JSONB,
            ai_review              JSONB,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_submissions_students_assignment_id
            ON submissions(students_assignment_id)
    """)
    op.execute("CREATE INDEX ix_submissions_status ON submissions(status)")
    op.execute("""
        CREATE TRIGGER trg_submissions_updated_at
            BEFORE UPDATE ON submissions
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # quizzes
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

    # outbox_messages
    op.execute("""
        CREATE TABLE outbox_messages (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_type    outbox_event_type NOT NULL,
            payload       JSONB NOT NULL,
            state         outbox_message_state NOT NULL DEFAULT 'PENDING',
            finished_at   TIMESTAMPTZ,
            retry_count   INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_outbox_messages_state_event_type
            ON outbox_messages(state, event_type)
    """)
    op.execute("CREATE INDEX ix_outbox_messages_pending ON outbox_messages(state, created_at)")
    op.execute("""
        CREATE TRIGGER trg_outbox_messages_updated_at
            BEFORE UPDATE ON outbox_messages
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


def downgrade() -> None:
    for table in [
        "quizzes",
        "submissions",
        "students_assignments",
        "subjects_assignments",
        "subjects_students",
        "students",
        "subjects",
        "groups",
        "outbox_messages",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    for enum_type in [
        "submission_status",
        "submission_source_type",
        "outbox_event_type",
        "outbox_message_state",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")

    op.execute("DROP FUNCTION IF EXISTS set_updated_at")
