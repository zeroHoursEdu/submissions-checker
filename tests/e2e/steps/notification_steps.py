"""Step definitions for student notification preference flows."""

from __future__ import annotations

from pytest_bdd import given, then, when

from tests.e2e.pages.notifications_page import NotificationsPage


@given("I am on the notification preferences page")
def navigate_to_notifications(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    np.navigate()
    np.assert_on_page()


@given("the SUBMISSION_CHECKED email notification is currently enabled")
def ensure_email_enabled(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    state = np.get_toggle_state("SUBMISSION_CHECKED", "EMAIL")
    if state == "disabled":
        # Toggle it on first
        np.toggle_email("SUBMISSION_CHECKED")


@given("the SUBMISSION_CHECKED email notification is currently disabled")
def ensure_email_disabled(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    state = np.get_toggle_state("SUBMISSION_CHECKED", "EMAIL")
    if state == "enabled":
        # Toggle it off first
        np.toggle_email("SUBMISSION_CHECKED")


@when("I toggle the SUBMISSION_CHECKED EMAIL notification")
def toggle_submission_checked_email(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    np.toggle_email("SUBMISSION_CHECKED")


@then("the SUBMISSION_CHECKED EMAIL notification should be disabled")
def assert_email_disabled(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    state = np.get_toggle_state("SUBMISSION_CHECKED", "EMAIL")
    assert state == "disabled", f"Expected 'disabled' but got '{state}'"


@then("the SUBMISSION_CHECKED EMAIL notification should be enabled")
def assert_email_enabled(page, app_url: str) -> None:
    np = NotificationsPage(page, app_url)
    state = np.get_toggle_state("SUBMISSION_CHECKED", "EMAIL")
    assert state == "enabled", f"Expected 'enabled' but got '{state}'"
