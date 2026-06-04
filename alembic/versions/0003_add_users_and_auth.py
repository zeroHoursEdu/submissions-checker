"""Add users table with roles and seed accounts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-30

"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_dev() -> bool:
    return os.environ.get("ENVIRONMENT", "production") == "development"


def upgrade() -> None:
    conn = op.get_bind()

    # Create user_role enum
    conn.execute(text("CREATE TYPE user_role AS ENUM ('TEACHER', 'STUDENT')"))

    # Create users table
    conn.execute(
        text("""
        CREATE TABLE users (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            username      VARCHAR(100) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role          user_role NOT NULL,
            student_id    BIGINT REFERENCES students(id) ON DELETE SET NULL,
            is_active     BOOLEAN NOT NULL DEFAULT true,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_users_username   UNIQUE (username),
            CONSTRAINT uq_users_student_id UNIQUE (student_id),
            CONSTRAINT ck_users_student_role_has_student_id
                CHECK ((role = 'STUDENT' AND student_id IS NOT NULL) OR role = 'TEACHER')
        )
        """)
    )

    conn.execute(text("CREATE INDEX ix_users_username   ON users(username)"))
    conn.execute(text("CREATE INDEX ix_users_student_id ON users(student_id)"))
    conn.execute(
        text("""
        CREATE TRIGGER trg_users_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)
    )

    if not _is_dev():
        return

    # Pre-computed bcrypt(rounds=12) hashes — generated offline:
    #   python -c "import bcrypt; print(bcrypt.hashpw(b'teacher123', bcrypt.gensalt(12)).decode())"
    TEACHER_HASH = "$2b$12$owTWm1PkZw1iRKfLExVSqeG3xy0OKEXbM.mcsypqRNw7CRCeMSgmC"
    #   python -c "import bcrypt; print(bcrypt.hashpw(b'student123', bcrypt.gensalt(12)).decode())"
    STUDENT_HASH = "$2b$12$GPjjX3rKnpv/NnM.ONE/euzpOafZZHnCeyE8lYnyyNtgGOMf9Mo36"

    # Teacher account
    conn.execute(
        text(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES ('teacher', :h, 'TEACHER')"
        ),
        {"h": TEACHER_HASH},
    )

    # Student accounts — resolve student_id by email
    for username, email in [
        ("ivan",   "ivan@example.com"),
        ("olena",  "olena@example.com"),
        ("mykola", "mykola@example.com"),
    ]:
        student_id = conn.execute(
            text("SELECT id FROM students WHERE email = :e"), {"e": email}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, student_id) "
                "VALUES (:u, :h, 'STUDENT', :sid)"
            ),
            {"u": username, "h": STUDENT_HASH, "sid": student_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _is_dev():
        conn.execute(text("DELETE FROM users"))

    conn.execute(text("DROP TRIGGER IF EXISTS trg_users_updated_at ON users"))
    conn.execute(text("DROP TABLE users"))
    conn.execute(text("DROP TYPE user_role"))
