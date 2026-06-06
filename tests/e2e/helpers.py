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
STUDENT_EMAIL = "e2e.student@test.example"


def db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(E2E_DB_URL)


_STATUS_ALIASES = {
    "FAILED": {"FAILED", "TEST_FAILED", "CHECK_FAILED", "VALIDATION_FAILED"},
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


def get_student_credentials_from_outbox(email: str) -> dict | None:
    """Read student credentials from outbox_messages (plaintext password stored in payload)."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM outbox_messages
                WHERE event_type = 'SEND_CREDENTIALS'
                  AND payload->>'student_email' = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()
            return row[0] if row else None
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
