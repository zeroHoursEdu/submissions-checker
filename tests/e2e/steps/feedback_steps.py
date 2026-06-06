"""Step definitions for feedback request and response flows."""

from __future__ import annotations

import secrets
from datetime import date

from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import db_conn as _db_conn, get_feedback_token_for_subject
from tests.e2e.pages.feedback_page import StudentFeedbackPage, TeacherFeedbackPage


def _ensure_semester_exists() -> int:
    """Insert a current-active semester if none exists. Returns semester ID."""
    conn = _db_conn()
    today = date.today()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM semesters WHERE start_date <= %s AND end_date >= %s LIMIT 1",
                (today, today),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                """
                INSERT INTO semesters (name, season, start_date, end_date)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    f"E2E Semester {today.year}",
                    "FALL" if today.month >= 9 else "SPRING",
                    date(today.year, 1, 1),
                    date(today.year, 12, 31),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]
    finally:
        conn.close()


def _get_latest_feedback_token(subject_id: int) -> str | None:
    conn = _db_conn()
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


def _feedback_request_exists(subject_id: int) -> bool:
    conn = _db_conn()
    today = date.today()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT fr.id FROM feedback_requests fr
                JOIN semesters s ON s.id = fr.semester_id
                WHERE fr.subject_id = %s AND s.start_date <= %s AND s.end_date >= %s
                """,
                (subject_id, today, today),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


@given("a semester exists in the database")
def ensure_semester(e2e_context: dict) -> None:
    semester_id = _ensure_semester_exists()
    e2e_context["semester_id"] = semester_id


def _delete_feedback_data_for_subject(subject_id: int) -> None:
    """Remove all feedback requests/tokens for the subject so a fresh one can be created."""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM feedback_tokens WHERE feedback_request_id IN "
                "(SELECT id FROM feedback_requests WHERE subject_id = %s)",
                (subject_id,),
            )
            cur.execute("DELETE FROM feedback_requests WHERE subject_id = %s", (subject_id,))
            conn.commit()
    finally:
        conn.close()


@given("a feedback request exists for the E2E test subject")
def ensure_feedback_request(page, app_url: str, e2e_context: dict, teacher_account: dict) -> None:
    subject_id = e2e_context["subject_id"]
    _ensure_semester_exists()
    # Always delete old feedback data so we get a fresh unused token each run
    if not e2e_context.get("_feedback_cleaned"):
        _delete_feedback_data_for_subject(subject_id)
        e2e_context["_feedback_cleaned"] = True
    if not _feedback_request_exists(subject_id):
        from tests.e2e.pages.login_page import LoginPage
        lp = LoginPage(page, app_url)
        lp.navigate()
        lp.login(teacher_account["username"], teacher_account["password"])
        tfp = TeacherFeedbackPage(page, app_url)
        tfp.request_feedback_for_subject(subject_id)


@given("a feedback token URL is available")
def load_feedback_token(e2e_context: dict, app_url: str) -> None:
    subject_id = e2e_context["subject_id"]
    token = get_feedback_token_for_subject(subject_id)
    assert token, "No feedback token found — was a feedback request created?"
    e2e_context["feedback_token"] = token
    e2e_context["feedback_token_url"] = f"{app_url}/feedback/{token}"


@when("I click the Request Feedback button")
def click_request_feedback(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    # Clean up so the feedback request form is available (not already sent this semester)
    if not e2e_context.get("_feedback_cleaned"):
        _delete_feedback_data_for_subject(subject_id)
        e2e_context["_feedback_cleaned"] = True
    tfp = TeacherFeedbackPage(page, app_url)
    tfp.request_feedback_for_subject(subject_id)


@when("I navigate to the feedback token URL as an anonymous user")
def navigate_to_feedback_token(page, app_url: str, e2e_context: dict) -> None:
    token = e2e_context.get("feedback_token")
    assert token, "Feedback token not in context"
    sfp = StudentFeedbackPage(page, app_url)
    sfp.navigate_to_feedback_form(token)


@when(parsers.parse("I submit the feedback form with a rating of {rating:d}"))
def submit_feedback_form(page, app_url: str, e2e_context: dict, rating: int) -> None:
    sfp = StudentFeedbackPage(page, app_url)
    sfp.submit_feedback(rating=rating)


@then("a feedback request should be created for the subject")
def assert_feedback_request_created(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    assert _feedback_request_exists(subject_id), "Feedback request was not created in the DB"


@then("I should be redirected to the thank you page")
def assert_thank_you_page(page, app_url: str) -> None:
    sfp = StudentFeedbackPage(page, app_url)
    sfp.assert_submitted_successfully()
