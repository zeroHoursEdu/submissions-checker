"""Step definitions for authentication flows (teacher and student)."""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import (
    TEACHER_USERNAME,
    get_student_credentials_from_outbox,
)
from tests.e2e.pages.login_page import LoginPage


@given("the teacher account exists in the database")
def teacher_account_seeded(teacher_account: dict) -> None:
    assert teacher_account["username"] == TEACHER_USERNAME


@given("I am on the login page")
def navigate_to_login(page, app_url: str) -> None:
    lp = LoginPage(page, app_url)
    lp.navigate()


@given("I am logged in as the teacher")
def log_in_as_teacher(page, app_url: str, teacher_account: dict) -> None:
    # A prior scenario may already hold an auth cookie (the session/browser context is
    # shared); GET /auth/login would then redirect away from the form, so the #username
    # field never appears. Clear the session first so the login form is always shown.
    page.context.clear_cookies()
    lp = LoginPage(page, app_url)
    lp.navigate()
    lp.login(teacher_account["username"], teacher_account["password"])
    lp.assert_on_teacher_dashboard()


@given("I am logged in as the admin")
def log_in_as_admin(page, app_url: str, admin_account: dict) -> None:
    # A prior scenario may already hold an auth cookie; GET /auth/login would then
    # redirect away (no #username field), so clear any session first.
    page.context.clear_cookies()
    lp = LoginPage(page, app_url)
    lp.navigate()
    lp.login(admin_account["username"], admin_account["password"])
    # ADMIN role redirects to /portal on login (which needs a student record), so we
    # don't assert a landing page here — the auth cookie is set regardless and the
    # analytics routes are reachable directly.
    page.wait_for_load_state("networkidle")


@given("I am logged in as the student")
def log_in_as_student(page, app_url: str, e2e_context: dict) -> None:
    username = e2e_context.get("student_username")
    password = e2e_context.get("student_password")
    assert username and password, "Student credentials not in context — run enrollment first"
    page.context.clear_cookies()
    lp = LoginPage(page, app_url)
    lp.navigate()
    lp.login(username, password)
    lp.assert_on_student_portal()


@given("I have the student's generated credentials")
def load_student_credentials(e2e_context: dict) -> None:
    creds = get_student_credentials_from_outbox("e2e.student@test.example")
    if creds:
        e2e_context["student_username"] = creds["username"]
        e2e_context["student_password"] = creds["password"]


@when("I submit the login form with the teacher credentials")
def submit_teacher_credentials(page, app_url: str, teacher_account: dict) -> None:
    lp = LoginPage(page, app_url)
    lp.login(teacher_account["username"], teacher_account["password"])


@when("I submit the login form with the student credentials")
def submit_student_credentials(page, app_url: str, e2e_context: dict) -> None:
    lp = LoginPage(page, app_url)
    lp.login(e2e_context["student_username"], e2e_context["student_password"])


@when(parsers.parse('I submit the login form with username "{username}" and password "{password}"'))
def submit_wrong_credentials(page, app_url: str, username: str, password: str) -> None:
    lp = LoginPage(page, app_url)
    lp.login(username, password)


@then("I should be redirected to the teacher dashboard")
def assert_teacher_dashboard(page, app_url: str) -> None:
    lp = LoginPage(page, app_url)
    lp.assert_on_teacher_dashboard()


@then("I should be redirected to the student portal")
def assert_student_portal(page, app_url: str) -> None:
    lp = LoginPage(page, app_url)
    lp.assert_on_student_portal()


@then("I should see a login error message")
def assert_login_error(page, app_url: str) -> None:
    lp = LoginPage(page, app_url)
    lp.assert_login_error()


@then("I should remain on the login page")
def assert_still_on_login(page, app_url: str) -> None:
    assert "/login" in page.url
