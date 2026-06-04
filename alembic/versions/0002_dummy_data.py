"""Dummy data for local development — no-op in production.

Inserts two subjects, one student group, three students, a set of assignments,
enrollments, and a handful of submissions with varied statuses so the student
portal has something to browse immediately.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-30

"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_dev() -> bool:
    return os.environ.get("ENVIRONMENT", "production") == "development"


def upgrade() -> None:
    if not _is_dev():
        return

    conn = op.get_bind()

    # ── Groups ───────────────────────────────────────────────────────────────
    group_id = conn.execute(
        text(
            "INSERT INTO groups (name, description) "
            "VALUES ('IT-21', 'Software Engineering 2021 cohort') "
            "RETURNING id"
        )
    ).scalar_one()

    # ── Students ─────────────────────────────────────────────────────────────
    students_raw = [
        ("Ivan Petrenko",    "ivan@example.com",    "ivan-petrenko"),
        ("Olena Kovalenko",  "olena@example.com",   "olena-kovalenko"),
        ("Mykola Shevchenko","mykola@example.com",  "mykola-shevchenko"),
    ]
    student_ids: dict[str, int] = {}
    for full_name, email, github in students_raw:
        sid = conn.execute(
            text(
                "INSERT INTO students (group_id, github_username, email, full_name) "
                "VALUES (:gid, :github, :email, :name) RETURNING id"
            ),
            {"gid": group_id, "github": github, "email": email, "name": full_name},
        ).scalar_one()
        student_ids[github] = sid

    # ── Subjects ─────────────────────────────────────────────────────────────
    python_id = conn.execute(
        text(
            "INSERT INTO subjects (name, description, github_repo) "
            "VALUES ('Python Programming', "
            "        'Introduction to Python — data types, functions, OOP', "
            "        'https://github.com/example/python-labs') "
            "RETURNING id"
        )
    ).scalar_one()

    db_id = conn.execute(
        text(
            "INSERT INTO subjects (name, description) "
            "VALUES ('Database Design', "
            "        'Relational databases, SQL, and query optimisation') "
            "RETURNING id"
        )
    ).scalar_one()

    # ── Enrol all students in both subjects ──────────────────────────────────
    for sid in student_ids.values():
        for subj in (python_id, db_id):
            conn.execute(
                text(
                    "INSERT INTO subjects_students (subject_id, student_id) "
                    "VALUES (:s, :u)"
                ),
                {"s": subj, "u": sid},
            )

    # ── Assignments — Python Programming ─────────────────────────────────────
    py_assignments = [
        {
            "title": "Lab 1 — Hello World & Basic Types",
            "description": (
                "Write a program that greets the user and demonstrates int, float, "
                "str, and bool variables. Use f-strings for output.\n\n"
                "Requirements:\n"
                "1. Ask for the user's name.\n"
                "2. Print a personalised greeting.\n"
                "3. Show three arithmetic results.\n"
                "4. Check if an input number is even or odd."
            ),
            "deadline": "2026-02-01 23:59:00+00",
            "config": (
                '{"download_links": ['
                '{"label": "Lab 1 Task Sheet.pdf", "url": "https://example.com/py_lab1.pdf"}]}'
            ),
        },
        {
            "title": "Lab 2 — Collections & Comprehensions",
            "description": (
                "Implement a small student-grade tracker using lists, dicts, and sets.\n\n"
                "Requirements:\n"
                "1. Store grades in a dict keyed by student name.\n"
                "2. Compute average, min, max using list comprehensions.\n"
                "3. Find students who passed (grade >= 60).\n"
                "4. Export the results as a formatted table."
            ),
            "deadline": "2026-02-15 23:59:00+00",
            "config": (
                '{"download_links": ['
                '{"label": "Lab 2 Task Sheet.pdf", "url": "https://example.com/py_lab2.pdf"},'
                '{"label": "Starter Code.zip", "url": "https://example.com/py_lab2_starter.zip"}]}'
            ),
        },
        {
            "title": "Lab 3 — Functions & Modules",
            "description": (
                "Refactor the grade tracker from Lab 2 into reusable functions "
                "split across multiple modules.\n\n"
                "Requirements:\n"
                "1. Separate I/O, logic, and report modules.\n"
                "2. Write docstrings for every public function.\n"
                "3. Add at least 5 unit tests with pytest."
            ),
            "deadline": "2026-03-01 23:59:00+00",
            "config": "{}",
        },
    ]

    py_asgn_ids: list[int] = []
    for a in py_assignments:
        aid = conn.execute(
            text(
                "INSERT INTO subjects_assignments "
                "  (subject_id, title, description, deadline, config) "
                "VALUES (:sub, :title, :desc, CAST(:dl AS timestamptz), CAST(:cfg AS jsonb)) "
                "RETURNING id"
            ),
            {"sub": python_id, "title": a["title"], "desc": a["description"],
             "dl": a["deadline"], "cfg": a["config"]},
        ).scalar_one()
        py_asgn_ids.append(aid)

    # ── Assignments — Database Design ─────────────────────────────────────────
    db_assignments = [
        {
            "title": "Lab 1 — ER Diagrams",
            "description": (
                "Design an entity-relationship diagram for a library management system.\n\n"
                "Requirements:\n"
                "1. Identify at least 6 entities.\n"
                "2. Define cardinalities (1:1, 1:N, M:N).\n"
                "3. Mark primary and foreign keys.\n"
                "4. Submit as a PDF using draw.io or Lucidchart."
            ),
            "deadline": "2026-02-08 23:59:00+00",
            "config": (
                '{"download_links": ['
                '{"label": "ERD Guide.pdf", "url": "https://example.com/erd_guide.pdf"}]}'
            ),
        },
        {
            "title": "Lab 2 — SQL Queries",
            "description": (
                "Write 15 SQL queries against the provided PostgreSQL dataset.\n\n"
                "Must include:\n"
                "- 3 × multi-table JOINs\n"
                "- 3 × aggregations with GROUP BY / HAVING\n"
                "- 2 × correlated sub-queries\n"
                "- 1 × window function\n"
                "- EXPLAIN ANALYSE for the two slowest queries."
            ),
            "deadline": "2026-02-22 23:59:00+00",
            "config": (
                '{"download_links": ['
                '{"label": "Dataset Dump.sql", "url": "https://example.com/dataset.sql"},'
                '{"label": "Query Template.sql", "url": "https://example.com/template.sql"}]}'
            ),
        },
    ]

    db_asgn_ids: list[int] = []
    for a in db_assignments:
        aid = conn.execute(
            text(
                "INSERT INTO subjects_assignments "
                "  (subject_id, title, description, deadline, config) "
                "VALUES (:sub, :title, :desc, CAST(:dl AS timestamptz), CAST(:cfg AS jsonb)) "
                "RETURNING id"
            ),
            {"sub": db_id, "title": a["title"], "desc": a["description"],
             "dl": a["deadline"], "cfg": a["config"]},
        ).scalar_one()
        db_asgn_ids.append(aid)

    # ── students_assignments ──────────────────────────────────────────────────
    # Ivan:   Py-L1 graded 88 | Py-L2 submitted (reviewing) | Py-L3 not started
    #         DB-L1 graded 75 | DB-L2 not started
    # Olena:  Py-L1 graded 95 | Py-L2 graded 90 | Py-L3 graded 85
    #         DB-L1 graded 92 | DB-L2 submitted (pending)
    # Mykola: Py-L1 graded 72 | others not started

    ivan   = student_ids["ivan-petrenko"]
    olena  = student_ids["olena-kovalenko"]
    mykola = student_ids["mykola-shevchenko"]

    sa_rows: list[tuple[int, int, int | None]] = [
        (ivan,   py_asgn_ids[0], 88),
        (ivan,   py_asgn_ids[1], None),
        (ivan,   py_asgn_ids[2], None),
        (ivan,   db_asgn_ids[0], 75),
        (ivan,   db_asgn_ids[1], None),
        (olena,  py_asgn_ids[0], 95),
        (olena,  py_asgn_ids[1], 90),
        (olena,  py_asgn_ids[2], 85),
        (olena,  db_asgn_ids[0], 92),
        (olena,  db_asgn_ids[1], None),
        (mykola, py_asgn_ids[0], 72),
        (mykola, py_asgn_ids[1], None),
        (mykola, py_asgn_ids[2], None),
        (mykola, db_asgn_ids[0], None),
        (mykola, db_asgn_ids[1], None),
    ]

    sa_ids: dict[tuple[int, int], int] = {}
    for stu, asgn, grade in sa_rows:
        params: dict[str, object] = {"stu": stu, "asgn": asgn}
        if grade is not None:
            params["grade"] = grade
            row_id = conn.execute(
                text(
                    "INSERT INTO students_assignments "
                    "  (student_id, subjects_assignment_id, grade) "
                    "VALUES (:stu, :asgn, :grade) RETURNING id"
                ),
                params,
            ).scalar_one()
        else:
            row_id = conn.execute(
                text(
                    "INSERT INTO students_assignments "
                    "  (student_id, subjects_assignment_id) "
                    "VALUES (:stu, :asgn) RETURNING id"
                ),
                params,
            ).scalar_one()
        sa_ids[(stu, asgn)] = row_id

    # ── Submissions (varied statuses for UI variety) ──────────────────────────
    submissions = [
        # Ivan – Lab 2 Python: submitted via GitHub PR, currently under review
        (sa_ids[(ivan, py_asgn_ids[1])], "GITHUB_PR", "REVIEWING"),
        # Olena – Lab 2 DB: uploaded ZIP, waiting to be processed
        (sa_ids[(olena, db_asgn_ids[1])], "ZIP_UPLOAD", "PENDING"),
        # Mykola – Lab 1 DB: submitted via GitHub PR, failed
        (sa_ids[(mykola, db_asgn_ids[0])], "GITHUB_PR", "FAILED"),
    ]
    for sa_id, src, status in submissions:
        conn.execute(
            text(
                "INSERT INTO submissions (students_assignment_id, source_type, status) "
                "VALUES (:sa, CAST(:src AS submission_source_type), CAST(:status AS submission_status))"
            ),
            {"sa": sa_id, "src": src, "status": status},
        )


def downgrade() -> None:
    if not _is_dev():
        return

    conn = op.get_bind()
    conn.execute(text("DELETE FROM submissions"))
    conn.execute(text("DELETE FROM students_assignments"))
    conn.execute(text("DELETE FROM subjects_students"))
    conn.execute(text("DELETE FROM subjects_assignments"))
    conn.execute(text("DELETE FROM subjects"))
    conn.execute(text("DELETE FROM students"))
    conn.execute(text("DELETE FROM groups"))
