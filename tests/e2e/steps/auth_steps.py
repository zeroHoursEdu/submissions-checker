"""Step definitions for authentication flows (teacher and student)."""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import (
    TEACHER_PASSWORD,
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
    lp = LoginPage(page, app_url)
    lp.navigate()
    lp.login(teacher_account["username"], teacher_account["password"])
    lp.assert_on_teacher_dashboard()


@given("I am logged in as the student")
def log_in_as_student(page, app_url: str, e2e_context: dict) -> None:
    username = e2e_context.get("student_username")
    password = e2e_context.get("student_password")
    assert username and password, "Student credentials not in context — run enrollment first"
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
