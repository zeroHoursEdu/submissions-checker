"""BDD test runner for notifications.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import auth_steps  # noqa: F401
from tests.e2e.steps import enrollment_steps  # noqa: F401
from tests.e2e.steps import notification_steps  # noqa: F401
from tests.e2e.steps import subject_steps  # noqa: F401

scenarios("features/notifications.feature")
