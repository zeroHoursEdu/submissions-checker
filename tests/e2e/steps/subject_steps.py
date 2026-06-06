"""Step definitions for subject management flows."""

from __future__ import annotations

import io
import zipfile

from playwright.sync_api import expect
from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import E2E_DB_URL, db_conn as _db_conn
from tests.e2e.pages.analytics_page import AnalyticsPage
from tests.e2e.pages.teacher_dashboard import TeacherDashboard

FIXTURES_DIR = __import__("pathlib").Path(__file__).parent.parent / "fixtures"


def _get_subject_id_by_code(code: str) -> int | None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM subjects WHERE name = %s", (code,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


@given("the E2E test subject exists")
def ensure_subject_exists(page, app_url: str, e2e_context: dict, teacher_account: dict) -> None:
    """Upload the subject config ZIP if the subject is not yet in the DB."""
    subject_id = _get_subject_id_by_code("E2E Test Subject")
    if subject_id is None:
        # Need to log in and upload
        from tests.e2e.pages.login_page import LoginPage
        lp = LoginPage(page, app_url)
        lp.navigate()
        lp.login(teacher_account["username"], teacher_account["password"])
        lp.assert_on_teacher_dashboard()
        td = TeacherDashboard(page, app_url)
        td.upload_subject_config(FIXTURES_DIR / "sample_subject.zip")
        # re-fetch
        subject_id = _get_subject_id_by_code("E2E Test Subject")

    assert subject_id is not None, "Subject should exist after ZIP upload"
    e2e_context["subject_id"] = subject_id
    e2e_context["subject_name"] = "E2E Test Subject"


@given("I am on the teacher dashboard")
def navigate_to_dashboard(page, app_url: str) -> None:
    td = TeacherDashboard(page, app_url)
    td.navigate()


@given("I am on the subject page for the E2E test subject")
def navigate_to_subject_page(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context.get("subject_id")
    assert subject_id, "subject_id not in context"
    page.goto(f"{app_url}/teacher/subjects/{subject_id}")


@when("I upload the sample subject config ZIP")
def upload_subject_zip(page, app_url: str) -> None:
    td = TeacherDashboard(page, app_url)
    td.upload_subject_config(FIXTURES_DIR / "sample_subject.zip")


@when("I upload an invalid ZIP file")
def upload_invalid_zip(page, app_url: str) -> None:
    """Create a temp file that is not a valid ZIP and upload it."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(b"not a real zip file content")
        tmp_path = tmp.name
    td = TeacherDashboard(page, app_url)
    # Use the file input directly
    file_input = page.locator('input[name="config_zip"]')
    file_input.set_input_files(tmp_path)
    page.wait_for_url(f"{app_url}/teacher**")


@then(parsers.parse('the subject "{subject_name}" should appear on the dashboard'))
def assert_subject_on_dashboard(page, app_url: str, subject_name: str) -> None:
    td = TeacherDashboard(page, app_url)
    td.navigate()
    td.assert_subject_visible(subject_name)


@then("an error message should be visible on the dashboard")
def assert_dashboard_error(page, app_url: str) -> None:
    td = TeacherDashboard(page, app_url)
    td.assert_apply_error_visible()


@when("I navigate to the analytics page")
def navigate_to_analytics(page, app_url: str) -> None:
    ap = AnalyticsPage(page, app_url)
    ap.navigate()


@when("I navigate to the fraud analytics page")
def navigate_to_fraud_analytics(page, app_url: str) -> None:
    page.goto(f"{app_url}/teacher/analytics/fraud")


@then("the analytics page should load without errors")
def assert_analytics_loaded(page, app_url: str) -> None:
    ap = AnalyticsPage(page, app_url)
    ap.assert_on_analytics()
    ap.assert_no_error()


@then("statistical content should be visible on the page")
def assert_analytics_content(page, app_url: str) -> None:
    ap = AnalyticsPage(page, app_url)
    ap.assert_stats_visible()


@then("the fraud analytics page should load without errors")
def assert_fraud_analytics_loaded(page, app_url: str) -> None:
    page.wait_for_load_state("networkidle")
    assert "Internal Server Error" not in page.content()
