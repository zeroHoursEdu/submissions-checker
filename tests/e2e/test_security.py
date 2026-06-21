"""BDD test runner for security.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import auth_steps  # noqa: F401
from tests.e2e.steps import enrollment_steps  # noqa: F401
from tests.e2e.steps import feedback_steps  # noqa: F401
from tests.e2e.steps import security_steps  # noqa: F401
from tests.e2e.steps import subject_steps  # noqa: F401

scenarios("features/security.feature")
