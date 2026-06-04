"""Add composite indexes to support analytics dashboard queries.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Composite covering index for per-assignment grade aggregation.
    # Supports: GROUP BY sa.subjects_assignment_id with AVG/COUNT on grade.
    conn.execute(text("""
        CREATE INDEX ix_students_assignments_sa_id_grade
            ON students_assignments(subjects_assignment_id, grade)
    """))

    # Partial index for graded-only rows — accelerates pass-rate queries.
    # Supports: WHERE grade IS NOT NULL across analytics aggregations.
    conn.execute(text("""
        CREATE INDEX ix_students_assignments_grade_not_null
            ON students_assignments(student_id, grade)
            WHERE grade IS NOT NULL
    """))

    # Composite covering index for login analytics per user.
    # Supports: COUNT(*), MIN(logged_in_at), MAX(logged_in_at) GROUP BY user_id.
    conn.execute(text("""
        CREATE INDEX ix_user_logins_user_id_logged_in_at
            ON user_logins(user_id, logged_in_at)
    """))

    # Composite index for single-day submission detection.
    # Supports: GROUP BY students_assignment_id, DATE_TRUNC('day', created_at).
    conn.execute(text("""
        CREATE INDEX ix_submissions_sa_id_created_at
            ON submissions(students_assignment_id, created_at)
    """))

    # Composite index for cross-subject student profile lookup.
    # Supports: WHERE student_id = :sid JOIN subjects_assignments ON ...
    conn.execute(text("""
        CREATE INDEX ix_students_assignments_student_id_sa_id
            ON students_assignments(student_id, subjects_assignment_id)
    """))

    # Composite index for enrollment + subject lookup in fraud queries.
    # Supports: JOIN subjects_students WHERE subject_id = :sid AND student_id.
    conn.execute(text("""
        CREATE INDEX ix_subjects_students_subject_id_student_id
            ON subjects_students(subject_id, student_id)
    """))


def downgrade() -> None:
    conn = op.get_bind()
    for idx in [
        "ix_students_assignments_sa_id_grade",
        "ix_students_assignments_grade_not_null",
        "ix_user_logins_user_id_logged_in_at",
        "ix_submissions_sa_id_created_at",
        "ix_students_assignments_student_id_sa_id",
        "ix_subjects_students_subject_id_student_id",
    ]:
        conn.execute(text(f"DROP INDEX IF EXISTS {idx}"))
