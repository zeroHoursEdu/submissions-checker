"""BDD test runner for security.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import (
    auth_steps,  # noqa: F401
    enrollment_steps,  # noqa: F401
    feedback_steps,  # noqa: F401
    security_steps,  # noqa: F401
    subject_steps,  # noqa: F401
)

scenarios("features/security.feature")
