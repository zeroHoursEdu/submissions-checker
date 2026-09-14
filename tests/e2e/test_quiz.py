"""BDD test runner for quiz.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import (
    auth_steps,  # noqa: F401
    enrollment_steps,  # noqa: F401
    quiz_steps,  # noqa: F401
    subject_steps,  # noqa: F401
    submission_steps,  # noqa: F401
)

scenarios("features/quiz.feature")
