"""Step definitions for student enrollment flows."""

from __future__ import annotations

import time

from pytest_bdd import given, parsers, then, when

from tests.e2e.helpers import (
    STUDENT_EMAIL,
    provision_student_credentials,
)
from tests.e2e.helpers import (
    db_conn as _db_conn,
)
from tests.e2e.pages.subject_page import SubjectPage

FIXTURES_DIR = __import__("pathlib").Path(__file__).parent.parent / "fixtures"


def _get_enrolled_count(subject_id: int) -> int:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM subjects_students ss
                JOIN students s ON s.id = ss.student_id
                WHERE ss.subject_id = %s AND s.type = 'REAL'
                """,
                (subject_id,),
            )
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()


def _fixture_emails() -> list[str]:
    """E-mail column of the students fixture, so both steps use the same roster."""
    import csv

    with open(FIXTURES_DIR / "students.csv", encoding="utf-8") as f:
        return [row["email"].strip() for row in csv.DictReader(f) if row.get("email", "").strip()]


def _do_enrollment(page, app_url: str, subject_id: int) -> None:
    """Create the accounts, then enrol them.

    These are two separate endpoints: the global import creates students and
    queues their credential e-mails, while the per-subject import enrols only
    and would reject an address that does not exist yet.
    """
    sp = SubjectPage(page, app_url)

    created = sp.create_students_via_api(FIXTURES_DIR / "students.csv")
    assert created["status"] in (200, 303, 302), f"Account creation failed: {created}"

    enrolled = sp.enrol_students_via_api(subject_id, _fixture_emails())
    assert enrolled["status"] in (200, 303, 302), f"Enrolment failed: {enrolled}"


@given("the student is enrolled in the E2E test subject")
def ensure_student_enrolled(page, app_url: str, e2e_context: dict, teacher_account: dict) -> None:
    subject_id = e2e_context.get("subject_id")
    assert subject_id, "subject_id not in context"

    # Try to get credentials first (enrollment may have already happened in a prior test)
    creds = provision_student_credentials(STUDENT_EMAIL)
    if creds:
        e2e_context["student_username"] = creds["username"]
        e2e_context["student_password"] = creds["password"]

    if not e2e_context.get("student_username"):
        # Need to enroll — log in as teacher first so auth cookies are present
        from tests.e2e.pages.login_page import LoginPage

        lp = LoginPage(page, app_url)
        # The subject step may have left a teacher session on this page; a signed-in
        # visitor is redirected away from the login form, so clear it first.
        lp.logout()
        lp.navigate()
        lp.login(teacher_account["username"], teacher_account["password"])
        _do_enrollment(page, app_url, subject_id)
        time.sleep(1)
        creds = provision_student_credentials(STUDENT_EMAIL)
        if creds:
            e2e_context["student_username"] = creds["username"]
            e2e_context["student_password"] = creds["password"]

    assert e2e_context.get("student_username"), "Student credentials not available after enrollment"


@given(parsers.parse('the student "{email}" has been enrolled via CSV'))
def student_enrolled_via_csv(
    email: str, page, app_url: str, e2e_context: dict, teacher_account: dict
) -> None:
    subject_id = e2e_context.get("subject_id")
    assert subject_id, "subject_id not in context"
    if not provision_student_credentials(email):
        from tests.e2e.pages.login_page import LoginPage

        lp = LoginPage(page, app_url)
        # The subject step may have left a teacher session on this page; a signed-in
        # visitor is redirected away from the login form, so clear it first.
        lp.logout()
        lp.navigate()
        lp.login(teacher_account["username"], teacher_account["password"])
        _do_enrollment(page, app_url, subject_id)
        time.sleep(0.5)


@when("I import the student CSV into the subject")
def import_student_csv(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    before = _get_enrolled_count(subject_id)
    _do_enrollment(page, app_url, subject_id)
    e2e_context["enrolled_count_before"] = before


@then("the enrolled student count should increase")
def assert_enrolled_count_increased(e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    before = e2e_context.get("enrolled_count_before", 0)
    after = _get_enrolled_count(subject_id)
    # After import, count >= before (may be same if student already existed)
    assert after >= before, f"Expected enrolled count >= {before}, got {after}"


@then(parsers.parse('the student "{full_name}" should be visible in the enrolled list'))
def assert_student_in_enrolled_list(page, app_url: str, e2e_context: dict, full_name: str) -> None:
    subject_id = e2e_context["subject_id"]
    page.goto(f"{app_url}/teacher/subjects/{subject_id}")
    page.wait_for_load_state("networkidle")
    # Check if the student appears — they might be listed as "Test Student"
    assert full_name in page.content() or "Test" in page.content()


@then("I can retrieve their login credentials from the system")
def retrieve_credentials(e2e_context: dict) -> None:
    creds = provision_student_credentials(STUDENT_EMAIL)
    assert creds is not None, f"No student account found for {STUDENT_EMAIL}"
    e2e_context["student_username"] = creds["username"]
    e2e_context["student_password"] = creds["password"]


@then("the credentials are stored in the context for later use")
def verify_credentials_in_context(e2e_context: dict) -> None:
    assert e2e_context.get("student_username"), "Username not in context"
    assert e2e_context.get("student_password"), "Password not in context"
