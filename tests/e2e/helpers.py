"""Shared E2E test helpers — no circular imports here."""

from __future__ import annotations

import os
import time

import psycopg2

E2E_APP_URL = os.environ.get("E2E_APP_URL", "http://localhost:8001")
E2E_DB_URL = os.environ.get(
    "E2E_DB_URL",
    "postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e",
)

TEACHER_USERNAME = "e2e_teacher"
TEACHER_PASSWORD = "E2eTeacher#2024"
ADMIN_USERNAME = "e2e_admin"
ADMIN_PASSWORD = "E2eAdmin#2024"
STUDENT_EMAIL = "e2e.student@test.example"


def db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(E2E_DB_URL)


_STATUS_ALIASES = {
    "FAILED": {"FAILED", "TEST_FAILED", "VALIDATION_FAILED"},
    "PASSED": {"PASSED", "COMPLETED"},
    "QUIZ_SENT": {"QUIZ_SENT"},
}


def wait_for_submission_status(submission_id: int, expected: str, timeout: int = 60) -> bool:
    """Block until the submission reaches expected status (or any alias)."""
    accepted = _STATUS_ALIASES.get(expected, {expected})
    deadline = time.monotonic() + timeout
    conn = psycopg2.connect(E2E_DB_URL)
    try:
        while time.monotonic() < deadline:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM submissions WHERE id = %s", (submission_id,))
                row = cur.fetchone()
                if row and row[0] in accepted:
                    return True
            time.sleep(1.0)
        return False
    finally:
        conn.close()


def provision_student_credentials(email: str) -> dict | None:
    """Give the student account behind *email* a fresh known password and return it.

    Credentials are e-mailed once and never kept readable in the database (the
    outbox row holds them sealed until sent, then a placeholder), so a test cannot
    read them back. It can do what a teacher would: rotate the password. Returns
    None when no account exists for the address yet.
    """
    import secrets

    import bcrypt

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username
                FROM users u JOIN students s ON s.id = u.student_id
                WHERE s.email = %s
                """,
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            user_id, username = row
            password = secrets.token_urlsafe(9)
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode()
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
            )
        conn.commit()
        return {"username": username, "password": password}
    finally:
        conn.close()


def get_latest_submission_id(student_assignment_id: int) -> int | None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM submissions WHERE students_assignment_id = %s ORDER BY id DESC LIMIT 1",
                (student_assignment_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def get_feedback_token_for_subject(subject_id: int) -> str | None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ft.token
                FROM feedback_tokens ft
                JOIN feedback_requests fr ON fr.id = ft.feedback_request_id
                WHERE fr.subject_id = %s
                ORDER BY ft.id DESC
                LIMIT 1
                """,
                (subject_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()
