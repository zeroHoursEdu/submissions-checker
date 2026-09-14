"""Step definitions for permission & security scenarios.

These assert the *real* authorization outcome the app produces:
  * unauthenticated access  -> HTTP 401 (raw "Not authenticated", no login redirect)
  * wrong-role access       -> HTTP 403 (role gate in dependencies.py)
  * cross-teacher object     -> HTTP 403 (require_subject_access in authz.py)
  * bogus feedback token     -> HTTP 404 rejection page
  * used feedback token      -> "already submitted" page (single-use enforced)

Cookies are cleared before every login (the suite has had cookie-bleed between
scenarios — see auth_steps.py), so each role probe starts from a clean session.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

import bcrypt
from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import db_conn as _db_conn
from tests.e2e.pages.login_page import LoginPage
from tests.e2e.pages.security_page import SecurityPage

OTHER_TEACHER_USERNAME = "e2e_teacher_b"
OTHER_TEACHER_PASSWORD = "E2eTeacherB#2024"
OTHER_SUBJECT_CODE = "E2E-OTHER-TEACHER-SUBJECT"
OTHER_SUBJECT_NAME = "E2E Other Teacher Subject"


# --------------------------------------------------------------------------- #
# DB seeding helpers
# --------------------------------------------------------------------------- #
def _ensure_other_teacher() -> int:
    """Insert a second teacher (teacher B) if absent. Returns user id."""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (OTHER_TEACHER_USERNAME,))
            row = cur.fetchone()
            if row:
                return row[0]
            pw_hash = bcrypt.hashpw(OTHER_TEACHER_PASSWORD.encode(), bcrypt.gensalt(12)).decode()
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at, updated_at)
                VALUES (%s, %s, 'TEACHER', NOW(), NOW())
                RETURNING id
                """,
                (OTHER_TEACHER_USERNAME, pw_hash),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
    finally:
        conn.close()


def _ensure_subject_owned_by(owner_id: int) -> int:
    """Insert an ACTIVE subject owned by owner_id if absent. Returns subject id."""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, owner_id FROM subjects WHERE name = %s AND status = 'ACTIVE'",
                (OTHER_SUBJECT_NAME,),
            )
            row = cur.fetchone()
            if row:
                # Make sure ownership is correct (re-point at teacher B if needed).
                if row[1] != owner_id:
                    cur.execute(
                        "UPDATE subjects SET owner_id = %s WHERE id = %s",
                        (owner_id, row[0]),
                    )
                    conn.commit()
                return row[0]
            cur.execute(
                """
                INSERT INTO subjects (name, code, owner_id, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'ACTIVE', NOW(), NOW())
                RETURNING id
                """,
                (OTHER_SUBJECT_NAME, OTHER_SUBJECT_CODE, owner_id),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
    finally:
        conn.close()


def _mark_token_used(token: str) -> None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE feedback_tokens SET used_at = %s WHERE token = %s",
                (datetime.now(UTC), token),
            )
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Givens
# --------------------------------------------------------------------------- #
@given("I am logged out", target_fixture="security_page")
def logged_out(page, app_url: str) -> SecurityPage:
    page.context.clear_cookies()
    return SecurityPage(page, app_url)


@given("another teacher owns a subject in the database")
def other_teacher_owns_subject(e2e_context: dict) -> None:
    owner_id = _ensure_other_teacher()
    subject_id = _ensure_subject_owned_by(owner_id)
    e2e_context["other_teacher_subject_id"] = subject_id


@given("a bogus feedback token URL")
def bogus_feedback_token(e2e_context: dict) -> None:
    e2e_context["bogus_token"] = "nonexistent-" + secrets.token_urlsafe(16)


@given("the feedback token has already been used")
def feedback_token_marked_used(e2e_context: dict) -> None:
    token = e2e_context.get("feedback_token")
    assert token, "No feedback token in context — load one first"
    _mark_token_used(token)


# --------------------------------------------------------------------------- #
# Whens
# --------------------------------------------------------------------------- #
@when(parsers.parse('I navigate to the protected page "{path}"'), target_fixture="last_status")
def navigate_protected(page, app_url: str, path: str) -> int:
    return SecurityPage(page, app_url).status_for(path)


@when(parsers.parse("I navigate to the other teacher's subject page"), target_fixture="last_status")
def navigate_other_subject(page, app_url: str, e2e_context: dict) -> int:
    subject_id = e2e_context["other_teacher_subject_id"]
    return SecurityPage(page, app_url).status_for(f"/teacher/subjects/{subject_id}")


@when("I navigate to the bogus feedback token URL", target_fixture="last_status")
def navigate_bogus_token(page, app_url: str, e2e_context: dict) -> int:
    token = e2e_context["bogus_token"]
    return SecurityPage(page, app_url).status_for(f"/feedback/{token}")


@when("I navigate to the used feedback token URL", target_fixture="last_status")
def navigate_used_token(page, app_url: str, e2e_context: dict) -> int:
    token = e2e_context["feedback_token"]
    return SecurityPage(page, app_url).status_for(f"/feedback/{token}")


@when("I log out")
def do_logout(page, app_url: str) -> None:
    LoginPage(page, app_url).logout()


# --------------------------------------------------------------------------- #
# Thens
# --------------------------------------------------------------------------- #
@then("access should be blocked as unauthenticated")
def assert_unauthenticated(page, last_status: int) -> None:
    # The app's dependency layer returns a raw 401 for missing/invalid session
    # cookies (it does NOT redirect protected HTML pages to /auth/login).
    assert last_status == 401, f"Expected 401 Unauthorized, got {last_status}"
    body = page.inner_text("body")
    assert "Not authenticated" in body, f"Expected auth-rejection body, got: {body[:120]!r}"


@then("access should be forbidden")
def assert_forbidden(page, last_status: int) -> None:
    assert last_status == 403, f"Expected 403 Forbidden, got {last_status}"


@then(parsers.parse("the page should not show the teacher dashboard"))
def assert_not_teacher_dashboard(page, app_url: str) -> None:
    # A genuine teacher dashboard renders an upload form for the subject config ZIP.
    assert page.locator('input[name="config_zip"]').count() == 0, (
        "Forbidden response unexpectedly rendered the teacher dashboard upload form"
    )


@then("the feedback link should be rejected as not found")
def assert_token_not_found(page, last_status: int) -> None:
    assert last_status == 404, f"Expected 404 for bogus token, got {last_status}"
    # The rejection page must NOT render the feedback form.
    assert page.locator('textarea[name="went_well"]').count() == 0, (
        "Bogus token unexpectedly rendered the feedback form"
    )


@then("the feedback link should be rejected as already used")
def assert_token_already_used(page, last_status: int) -> None:
    assert last_status == 200, f"Expected 200 already-submitted page, got {last_status}"
    # Must NOT render the form again (single-use enforced) and must NOT be the
    # thank-you page; the 'already submitted' page is its own distinct view.
    assert page.locator('textarea[name="went_well"]').count() == 0, (
        "Used token unexpectedly rendered the feedback form again"
    )
    assert "/thanks" not in page.url
    # The 'already submitted' template uses an amber warning container.
    assert page.locator(".bg-amber-100").count() >= 1, (
        "Used token did not render the 'already submitted' rejection page"
    )
