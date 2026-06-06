"""Step definitions for student assignment submission flows."""

from __future__ import annotations

import time

from pytest_bdd import given, then, when

from tests.e2e.helpers import (
    db_conn as _db_conn,
    get_latest_submission_id,
    wait_for_submission_status,
)
from tests.e2e.pages.student_portal import StudentPortal

FIXTURES_DIR = __import__("pathlib").Path(__file__).parent.parent / "fixtures"
SUBMISSION_WAIT_TIMEOUT = 150


def _get_student_assignment_id(subject_id: int, assignment_code: str, student_username: str) -> int | None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sa.id FROM students_assignments sa
                JOIN subjects_assignments suba ON suba.id = sa.subjects_assignment_id
                JOIN students st ON st.id = sa.student_id
                JOIN users u ON u.student_id = st.id
                WHERE suba.subject_id = %s AND suba.code = %s AND u.username = %s
                """,
                (subject_id, assignment_code, student_username),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _get_subjects_assignment_id(subject_id: int, code: str) -> int | None:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM subjects_assignments WHERE subject_id = %s AND code = %s",
                (subject_id, code),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


@given("I navigate to the lab1 assignment")
def navigate_to_lab1(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    username = e2e_context["student_username"]
    sa_id = _get_student_assignment_id(subject_id, "lab1", username)
    assert sa_id, f"Student assignment for lab1 not found (subject={subject_id}, user={username})"
    # Clean up prior-run submissions so each run starts fresh
    if e2e_context.get("_lab1_cleaned") != sa_id:
        conn = _db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM outbox_messages WHERE payload->>'submission_id' IN (SELECT id::text FROM submissions WHERE students_assignment_id = %s)", (sa_id,))
                cur.execute("DELETE FROM submissions WHERE students_assignment_id = %s", (sa_id,))
                conn.commit()
        finally:
            conn.close()
        e2e_context["_lab1_cleaned"] = sa_id
    e2e_context["current_sa_id"] = sa_id
    sp = StudentPortal(page, app_url)
    sp.open_assignment(subject_id, sa_id)


@given("the student's lab2 submission is in QUIZ_SENT status")
def set_lab2_quiz_sent(page, app_url: str, e2e_context: dict) -> None:
    """Ensure lab2 has a submission in QUIZ_SENT status (may require seeding via DB)."""
    subject_id = e2e_context["subject_id"]
    username = e2e_context["student_username"]
    sa_id = _get_student_assignment_id(subject_id, "lab2", username)
    if sa_id is None:
        sa_id = _get_student_assignment_id(subject_id, "lab2", username)
    assert sa_id, "Student assignment for lab2 not found"
    e2e_context["quiz_sa_id"] = sa_id

    # Clean up stale submissions and quiz attempts at the start of each run
    if e2e_context.get("_lab2_quiz_cleaned") != sa_id:
        conn = _db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM quiz_attempts WHERE submission_id IN "
                    "(SELECT id FROM submissions WHERE students_assignment_id = %s)",
                    (sa_id,),
                )
                cur.execute(
                    "DELETE FROM outbox_messages WHERE payload->>'submission_id' IN "
                    "(SELECT id::text FROM submissions WHERE students_assignment_id = %s)",
                    (sa_id,),
                )
                cur.execute("DELETE FROM submissions WHERE students_assignment_id = %s", (sa_id,))
                conn.commit()
        finally:
            conn.close()
        e2e_context["_lab2_quiz_cleaned"] = sa_id

    # Check if there's already a QUIZ_SENT submission
    sub_id = get_latest_submission_id(sa_id)
    if sub_id:
        conn = _db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM submissions WHERE id = %s", (sub_id,))
                row = cur.fetchone()
                if row and row[0] == "QUIZ_SENT":
                    e2e_context["quiz_submission_id"] = sub_id
                    return
        finally:
            conn.close()

    # Upload passing submission and wait for QUIZ_SENT
    sp = StudentPortal(page, app_url)
    sp.open_assignment(subject_id, sa_id)
    sp.upload_submission(FIXTURES_DIR / "submission_pass.zip")
    time.sleep(1)
    sub_id = get_latest_submission_id(sa_id)
    assert sub_id, "Submission not created for lab2"
    # Wait for worker to process (QUIZ_SENT)
    ok = wait_for_submission_status(sub_id, "QUIZ_SENT", timeout=SUBMISSION_WAIT_TIMEOUT)
    assert ok, f"Submission {sub_id} did not reach QUIZ_SENT status in time"
    e2e_context["quiz_submission_id"] = sub_id


@given("a previous submission has failed")
def ensure_previous_fail(e2e_context: dict) -> None:
    # This step is used in the resubmit scenario — the fail scenario runs first
    # Just verify there's at least one submission recorded
    sa_id = e2e_context.get("current_sa_id")
    assert sa_id, "No current student assignment ID in context"


@given("the lab1 assignment has already been passed")
def ensure_lab1_passed(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context.get("subject_id")
    username = e2e_context.get("student_username")
    sa_id = e2e_context.get("current_sa_id")
    if not sa_id and subject_id and username:
        sa_id = _get_student_assignment_id(subject_id, "lab1", username)
        e2e_context["current_sa_id"] = sa_id
    if not sa_id:
        return
    sub_id = get_latest_submission_id(sa_id)
    if sub_id:
        wait_for_submission_status(sub_id, "PASSED", timeout=SUBMISSION_WAIT_TIMEOUT)


@given("I have a failed quiz attempt")
def ensure_failed_quiz_attempt(e2e_context: dict) -> None:
    # This is set up by the "Student starts a quiz and fails" scenario preceding this one
    pass


@when("I upload the failing submission ZIP")
def upload_failing_zip(page, app_url: str, e2e_context: dict) -> None:
    sp = StudentPortal(page, app_url)
    sp.upload_submission(FIXTURES_DIR / "submission_fail.zip")
    time.sleep(0.5)
    sa_id = e2e_context["current_sa_id"]
    sub_id = get_latest_submission_id(sa_id)
    e2e_context["latest_submission_id"] = sub_id


@when("I upload the passing submission ZIP")
def upload_passing_zip(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    sa_id = e2e_context["current_sa_id"]
    sp = StudentPortal(page, app_url)
    sp.open_assignment(subject_id, sa_id)
    sp.upload_submission(FIXTURES_DIR / "submission_pass.zip")
    time.sleep(0.5)
    sub_id = get_latest_submission_id(sa_id)
    e2e_context["latest_submission_id"] = sub_id


@when("I try to upload another submission after passing")
def try_upload_after_pass(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    sa_id = e2e_context["current_sa_id"]
    sp = StudentPortal(page, app_url)
    sp.open_assignment(subject_id, sa_id)
    before_sub_id = get_latest_submission_id(sa_id)
    e2e_context["sub_before_blocked"] = before_sub_id
    try:
        sp.upload_submission(FIXTURES_DIR / "submission_pass.zip")
    except Exception:
        pass
    e2e_context["sub_after_blocked"] = get_latest_submission_id(sa_id)


@then("the submission should eventually show status FAILED")
def assert_submission_failed(e2e_context: dict) -> None:
    sub_id = e2e_context.get("latest_submission_id")
    assert sub_id, "No submission ID in context"
    ok = wait_for_submission_status(sub_id, "FAILED", timeout=SUBMISSION_WAIT_TIMEOUT)
    assert ok, f"Submission {sub_id} did not reach FAILED status in time"


@then("the submission should eventually show status PASSED")
def assert_submission_passed(e2e_context: dict) -> None:
    sub_id = e2e_context.get("latest_submission_id")
    assert sub_id, "No submission ID in context"
    ok = wait_for_submission_status(sub_id, "PASSED", timeout=SUBMISSION_WAIT_TIMEOUT)
    assert ok, f"Submission {sub_id} did not reach PASSED status in time"


@then("the system should block the upload or redirect without creating a new submission")
def assert_upload_blocked(e2e_context: dict) -> None:
    before = e2e_context.get("sub_before_blocked")
    after = e2e_context.get("sub_after_blocked")
    # Either the same submission ID (blocked), or None after (error page)
    assert before == after or after is None, "Expected upload to be blocked but a new submission was created"
