"""BDD test runner for notifications.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import (
    auth_steps,  # noqa: F401
    enrollment_steps,  # noqa: F401
    notification_steps,  # noqa: F401
    subject_steps,  # noqa: F401
)

scenarios("features/notifications.feature")
