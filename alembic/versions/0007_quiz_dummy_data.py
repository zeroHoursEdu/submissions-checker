"""Dummy quiz templates and a QUIZ_SENT submission for local testing.

Creates:
  • QuizTemplate + 5 questions for Python Lab 2
  • QuizTemplate + 4 questions for Database Lab 1
  • Updates Ivan's Python-Lab-2 submission from REVIEWING → QUIZ_SENT
    so that logging in as ivan/student123 immediately surfaces a quiz.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-30
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_dev() -> bool:
    return os.environ.get("ENVIRONMENT", "production") == "development"


def upgrade() -> None:
    if not _is_dev():
        return

    conn = op.get_bind()

    # ── Resolve assignment IDs created in 0002 ────────────────────────────────
    py_lab2_id = conn.execute(
        text("SELECT id FROM subjects_assignments WHERE title = 'Lab 2 — Collections & Comprehensions'")
    ).scalar_one()

    db_lab1_id = conn.execute(
        text("SELECT id FROM subjects_assignments WHERE title = 'Lab 1 — ER Diagrams'")
    ).scalar_one()

    # ── Quiz config shared helper ─────────────────────────────────────────────
    def j(obj: object) -> str:
        return json.dumps(obj)

    # ══════════════════════════════════════════════════════════════════════════
    # 1. QuizTemplate — Python Lab 2: Collections & Comprehensions
    # ══════════════════════════════════════════════════════════════════════════
    py2_config = {
        "total_questions": 5,
        "time_limit_minutes": 10,
        "max_quiz_attempts": 3,
        "pass_threshold_pct": 0.6,
        "shuffle_questions": True,
        "shuffle_options": True,
        "show_correct_answers_after": True,
    }
    py2_tmpl_id = conn.execute(
        text(
            "INSERT INTO quiz_templates (subjects_assignment_id, version, config) "
            "VALUES (:sa, 1, CAST(:cfg AS jsonb)) RETURNING id"
        ),
        {"sa": py_lab2_id, "cfg": j(py2_config)},
    ).scalar_one()

    py2_questions = [
        # 0 — Single choice
        {
            "type": "SINGLE_CHOICE",
            "text": "Which Python built-in creates a new list by applying an expression to each element of an iterable?",
            "points": 1,
            "is_required": False,
            "sort_order": 0,
            "config": {
                "options": [
                    "map()",
                    "List comprehension",
                    "filter()",
                    "reduce()",
                ],
                "correct": 1,
            },
        },
        # 1 — Single choice
        {
            "type": "SINGLE_CHOICE",
            "text": "What is the time complexity of checking membership in a Python set?",
            "points": 1,
            "is_required": False,
            "sort_order": 1,
            "config": {
                "options": ["O(n)", "O(log n)", "O(1)", "O(n²)"],
                "correct": 2,
            },
        },
        # 2 — Multiple choice
        {
            "type": "MULTIPLE_CHOICE",
            "text": "Which of the following are valid ways to create an empty dictionary in Python? (select all that apply)",
            "points": 2,
            "is_required": True,
            "sort_order": 2,
            "config": {
                "options": [
                    "d = {}",
                    "d = dict()",
                    "d = []",
                    "d = set()",
                ],
                "correct": [0, 1],
            },
        },
        # 3 — Ordering
        {
            "type": "ORDERING",
            "text": "Put these steps in the correct order for building a frequency dict from a list:",
            "points": 2,
            "is_required": False,
            "sort_order": 3,
            "config": {
                "items": [
                    "Create an empty dict: freq = {}",
                    "Loop over the list: for item in lst",
                    "Check if key exists: if item in freq",
                    "Increment the counter: freq[item] += 1",
                    "Otherwise set to 1: else freq[item] = 1",
                ],
                "correct_order": [0, 1, 2, 3, 4],
            },
        },
        # 4 — True / False
        {
            "type": "TRUE_FALSE",
            "text": "A Python list comprehension runs faster than an equivalent for-loop appending to a list.",
            "points": 1,
            "is_required": False,
            "sort_order": 4,
            "config": {"correct": True},
        },
    ]

    for q in py2_questions:
        conn.execute(
            text(
                "INSERT INTO quiz_questions "
                "  (template_id, type, text, points, is_required, sort_order, config) "
                "VALUES (:tmpl, CAST(:type AS quiz_question_type), :text, :pts, :req, :ord, CAST(:cfg AS jsonb))"
            ),
            {
                "tmpl": py2_tmpl_id,
                "type": q["type"],
                "text": q["text"],
                "pts": q["points"],
                "req": q["is_required"],
                "ord": q["sort_order"],
                "cfg": j(q["config"]),
            },
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 2. QuizTemplate — Database Lab 1: ER Diagrams
    # ══════════════════════════════════════════════════════════════════════════
    db1_config = {
        "total_questions": 4,
        "time_limit_minutes": None,
        "max_quiz_attempts": 2,
        "pass_threshold_pct": 0.5,
        "shuffle_questions": False,
        "shuffle_options": True,
        "show_correct_answers_after": True,
    }
    db1_tmpl_id = conn.execute(
        text(
            "INSERT INTO quiz_templates (subjects_assignment_id, version, config) "
            "VALUES (:sa, 1, CAST(:cfg AS jsonb)) RETURNING id"
        ),
        {"sa": db_lab1_id, "cfg": j(db1_config)},
    ).scalar_one()

    db1_questions = [
        {
            "type": "SINGLE_CHOICE",
            "text": "In an ER diagram, which symbol represents a weak entity?",
            "points": 1,
            "is_required": False,
            "sort_order": 0,
            "config": {
                "options": [
                    "Single rectangle",
                    "Double rectangle",
                    "Diamond",
                    "Ellipse",
                ],
                "correct": 1,
            },
        },
        {
            "type": "TRUE_FALSE",
            "text": "A many-to-many (M:N) relationship can be directly implemented in a relational database without an intermediate table.",
            "points": 1,
            "is_required": True,
            "sort_order": 1,
            "config": {"correct": False},
        },
        {
            "type": "MULTIPLE_CHOICE",
            "text": "Which of the following are examples of a 1:N relationship? (select all that apply)",
            "points": 2,
            "is_required": False,
            "sort_order": 2,
            "config": {
                "options": [
                    "One department has many employees",
                    "One student has many courses (and vice versa)",
                    "One order contains many order items",
                    "One person has one passport",
                ],
                "correct": [0, 2],
            },
        },
        {
            "type": "ORDERING",
            "text": "Arrange the normal forms in the order they are applied during database normalisation:",
            "points": 2,
            "is_required": False,
            "sort_order": 3,
            "config": {
                "items": ["1NF — eliminate repeating groups", "2NF — remove partial dependencies", "3NF — remove transitive dependencies", "BCNF — stronger version of 3NF"],
                "correct_order": [0, 1, 2, 3],
            },
        },
    ]

    for q in db1_questions:
        conn.execute(
            text(
                "INSERT INTO quiz_questions "
                "  (template_id, type, text, points, is_required, sort_order, config) "
                "VALUES (:tmpl, CAST(:type AS quiz_question_type), :text, :pts, :req, :ord, CAST(:cfg AS jsonb))"
            ),
            {
                "tmpl": db1_tmpl_id,
                "type": q["type"],
                "text": q["text"],
                "pts": q["points"],
                "req": q["is_required"],
                "ord": q["sort_order"],
                "cfg": j(q["config"]),
            },
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 3. Give Ivan a QUIZ_SENT submission on Python Lab 2
    #    (his existing REVIEWING submission is updated; it was created in 0002)
    # ══════════════════════════════════════════════════════════════════════════
    ivan_id = conn.execute(
        text("SELECT id FROM students WHERE email = 'ivan@example.com'")
    ).scalar_one()

    ivan_py2_sa_id = conn.execute(
        text(
            "SELECT id FROM students_assignments "
            "WHERE student_id = :sid AND subjects_assignment_id = :asgn"
        ),
        {"sid": ivan_id, "asgn": py_lab2_id},
    ).scalar_one()

    # Update the existing REVIEWING submission → QUIZ_SENT
    conn.execute(
        text(
            "UPDATE submissions "
            "SET status = CAST('QUIZ_SENT' AS submission_status) "
            "WHERE students_assignment_id = :sa AND status = CAST('REVIEWING' AS submission_status)"
        ),
        {"sa": ivan_py2_sa_id},
    )

    # Also give Mykola a QUIZ_SENT submission on Database Lab 1
    # so a second test account can try the ER quiz
    mykola_id = conn.execute(
        text("SELECT id FROM students WHERE email = 'mykola@example.com'")
    ).scalar_one()

    mykola_db1_sa_id = conn.execute(
        text(
            "SELECT id FROM students_assignments "
            "WHERE student_id = :sid AND subjects_assignment_id = :asgn"
        ),
        {"sid": mykola_id, "asgn": db_lab1_id},
    ).scalar_one()

    conn.execute(
        text(
            "INSERT INTO submissions (students_assignment_id, source_type, status) "
            "VALUES (:sa, CAST('ZIP_UPLOAD' AS submission_source_type), CAST('QUIZ_SENT' AS submission_status))"
        ),
        {"sa": mykola_db1_sa_id},
    )


def downgrade() -> None:
    if not _is_dev():
        return

    conn = op.get_bind()

    # Remove quiz attempts and answers first (FK cascade handles quiz_answers)
    conn.execute(text("DELETE FROM quiz_attempts"))
    conn.execute(text("DELETE FROM quiz_questions"))
    conn.execute(text("DELETE FROM quiz_templates"))

    # Revert Ivan's Lab 2 submission back to REVIEWING
    ivan_id = conn.execute(
        text("SELECT id FROM students WHERE email = 'ivan@example.com'")
    ).scalar_one_or_none()
    if ivan_id:
        py_lab2_id = conn.execute(
            text("SELECT id FROM subjects_assignments WHERE title = 'Lab 2 — Collections & Comprehensions'")
        ).scalar_one_or_none()
        if py_lab2_id:
            ivan_py2_sa_id = conn.execute(
                text(
                    "SELECT id FROM students_assignments "
                    "WHERE student_id = :sid AND subjects_assignment_id = :asgn"
                ),
                {"sid": ivan_id, "asgn": py_lab2_id},
            ).scalar_one_or_none()
            if ivan_py2_sa_id:
                conn.execute(
                    text(
                        "UPDATE submissions SET status = CAST('REVIEWING' AS submission_status) "
                        "WHERE students_assignment_id = :sa AND status = CAST('QUIZ_SENT' AS submission_status)"
                    ),
                    {"sa": ivan_py2_sa_id},
                )

    # Remove Mykola's dummy DB Lab 1 submission
    mykola_id = conn.execute(
        text("SELECT id FROM students WHERE email = 'mykola@example.com'")
    ).scalar_one_or_none()
    if mykola_id:
        db_lab1_id = conn.execute(
            text("SELECT id FROM subjects_assignments WHERE title = 'Lab 1 — ER Diagrams'")
        ).scalar_one_or_none()
        if db_lab1_id:
            mykola_db1_sa_id = conn.execute(
                text(
                    "SELECT id FROM students_assignments "
                    "WHERE student_id = :sid AND subjects_assignment_id = :asgn"
                ),
                {"sid": mykola_id, "asgn": db_lab1_id},
            ).scalar_one_or_none()
            if mykola_db1_sa_id:
                conn.execute(
                    text(
                        "DELETE FROM submissions "
                        "WHERE students_assignment_id = :sa AND status = CAST('QUIZ_SENT' AS submission_status)"
                    ),
                    {"sa": mykola_db1_sa_id},
                )
