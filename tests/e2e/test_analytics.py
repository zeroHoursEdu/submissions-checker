"""BDD test runner for analytics.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import auth_steps  # noqa: F401
from tests.e2e.steps import subject_steps  # noqa: F401

scenarios("features/analytics.feature")
