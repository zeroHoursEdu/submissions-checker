"""BDD test runner for teacher_auth.feature."""

from pytest_bdd import scenarios

from tests.e2e.steps import auth_steps  # noqa: F401 — registers step definitions

scenarios("features/teacher_auth.feature")
