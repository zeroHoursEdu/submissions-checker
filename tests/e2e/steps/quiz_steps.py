"""Step definitions for student quiz flows."""

from __future__ import annotations

from pytest_bdd import given, then, when

from tests.e2e.pages.quiz_page import QuizPage


@when("I start the quiz for the lab2 assignment")
def start_quiz(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    sa_id = e2e_context["quiz_sa_id"]
    qp = QuizPage(page, app_url)
    qp.start_quiz(subject_id, sa_id)
    e2e_context["quiz_attempt_id"] = qp.get_attempt_id()


@when("I start another quiz attempt")
def start_another_quiz_attempt(page, app_url: str, e2e_context: dict) -> None:
    subject_id = e2e_context["subject_id"]
    sa_id = e2e_context["quiz_sa_id"]
    qp = QuizPage(page, app_url)
    qp.start_quiz(subject_id, sa_id)
    e2e_context["quiz_attempt_id"] = qp.get_attempt_id()


@when("I answer the quiz question incorrectly")
def answer_incorrectly(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.answer_first_question_incorrectly()


@when("I answer the quiz question correctly")
def answer_correctly(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.answer_first_question_correctly()


@when("I submit the quiz")
def submit_quiz(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.submit_quiz()


@then("the quiz result page should show a failed result")
def assert_quiz_failed(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.assert_failed()


@then("a retry option should be available")
def assert_retry_option(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.assert_retry_available()


@then("the quiz result page should show a passed result")
def assert_quiz_passed(page, app_url: str) -> None:
    qp = QuizPage(page, app_url)
    qp.assert_passed()
