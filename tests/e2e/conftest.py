"""E2E test configuration and shared fixtures.

Step modules are declared as pytest plugins so pytest auto-discovers
the step-definition fixtures without each test file re-importing them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import bcrypt
import pytest

from tests.e2e.helpers import (
    TEACHER_PASSWORD,
    TEACHER_USERNAME,
    db_conn,
)

# Declare step modules as pytest plugins — this is how pytest-bdd 8.x
# discovers step definition fixtures defined in separate modules.
pytest_plugins = [
    "tests.e2e.steps.auth_steps",
    "tests.e2e.steps.subject_steps",
    "tests.e2e.steps.enrollment_steps",
    "tests.e2e.steps.submission_steps",
    "tests.e2e.steps.notification_steps",
    "tests.e2e.steps.feedback_steps",
    "tests.e2e.steps.quiz_steps",
]

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def app_url() -> str:
    from tests.e2e.helpers import E2E_APP_URL
    return E2E_APP_URL


@pytest.fixture(scope="session")
def teacher_account() -> Generator[dict, None, None]:
    """Insert a teacher user directly into the E2E DB (if not already present)."""
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (TEACHER_USERNAME,))
            existing = cur.fetchone()
            if existing is None:
                password_hash = bcrypt.hashpw(
                    TEACHER_PASSWORD.encode(), bcrypt.gensalt(12)
                ).decode()
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, created_at, updated_at)
                    VALUES (%s, %s, 'TEACHER', NOW(), NOW())
                    """,
                    (TEACHER_USERNAME, password_hash),
                )
                conn.commit()
        yield {"username": TEACHER_USERNAME, "password": TEACHER_PASSWORD}
    finally:
        conn.close()


@pytest.fixture(scope="session")
def context_state() -> dict:
    """Session-scoped mutable dict shared across all scenarios."""
    return {}


@pytest.fixture(scope="function")
def e2e_context(context_state: dict) -> dict:
    """Function-scoped alias to the session state dict."""
    return context_state
