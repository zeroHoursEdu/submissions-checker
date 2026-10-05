"""Unit tests for core.config.Settings — secret-key validator and env properties.

Settings are constructed explicitly with the required fields so no .env parsing
or DB access is involved. A valid throwaway database_url is supplied.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from submissions_checker.core.config import Settings

DB_URL = "postgresql+asyncpg://u:p@localhost:5432/db"
STRONG_KEY = "f3a1c9e7b5d2486017aa44bc99ee1122aabbccddeeff00112233445566778899"


def _settings(**overrides):
    base = {"secret_key": STRONG_KEY, "database_url": DB_URL}
    base.update(overrides)
    # Never read the developer's .env: these tests describe explicit inputs only.
    return Settings(_env_file=None, **base)


# ── secret-key validator ──────────────────────────────────────────────────────


def test_accepts_strong_key() -> None:
    s = _settings()
    assert s.secret_key == STRONG_KEY


@pytest.mark.parametrize(
    "weak",
    [
        "your-secret-key-here-change-in-production",
        "my-your-secret-key-thing-padded-to-32-chars",  # substring match
        "YOUR-SECRET-KEY-but-uppercased-padded-32chars",  # case-insensitive substring
    ],
)
def test_rejects_placeholder_keys_with_placeholder_message(weak: str) -> None:
    """>=32-char placeholders pass min_length but are caught by the denylist validator."""
    assert len(weak) >= 32
    with pytest.raises(ValidationError) as exc:
        _settings(secret_key=weak)
    msg = str(exc.value).lower()
    assert "placeholder" in msg or "weak" in msg


@pytest.mark.parametrize("weak", ["changeme", "secret", "CHANGEME", "Secret"])
def test_rejects_short_denylisted_keys(weak: str) -> None:
    """Short denylisted keys are rejected (by min_length before the denylist runs)."""
    assert len(weak) < 32  # documents that min_length fires first
    with pytest.raises(ValidationError):
        _settings(secret_key=weak)


def test_rejects_too_short_key() -> None:
    with pytest.raises(ValidationError):
        _settings(secret_key="short")  # min_length=32


# ── environment-derived properties ────────────────────────────────────────────


def test_production_flags() -> None:
    s = _settings(environment="production")
    assert s.is_production is True
    assert s.is_development is False
    assert s.is_test is False
    assert s.cookie_secure is True


def test_development_flags() -> None:
    s = _settings(environment="development")
    assert s.is_development is True
    assert s.is_production is False
    assert s.cookie_secure is False


def test_test_environment_flags() -> None:
    s = _settings(environment="test")
    assert s.is_test is True
    assert s.is_production is False
    assert s.cookie_secure is False


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(environment="staging")


def test_defaults_present() -> None:
    s = _settings()
    assert s.environment == "development"
    assert s.scheduler_enabled is True
    assert s.s3_bucket_name == "submissions-checker"
    assert s.teacher_digest_enabled is True


def test_debug_is_refused_in_production() -> None:
    """FastAPI debug returns tracebacks to the client; never in production."""
    with pytest.raises(ValidationError, match="DEBUG"):
        _settings(environment="production", debug=True)


def test_debug_allowed_outside_production() -> None:
    assert _settings(environment="development", debug=True).debug is True


# ── Classroom / LLM grading ───────────────────────────────────────────────────


def test_classroom_disabled_by_default() -> None:
    assert _settings().classroom_enabled is False


def test_google_client_requires_encryption_key() -> None:
    with pytest.raises(ValueError, match="GOOGLE_TOKEN_ENCRYPTION_KEY"):
        _settings(google_client_id="id", google_client_secret="sec")


def test_classroom_enabled_with_client_and_key() -> None:
    s = _settings(
        google_client_id="id", google_client_secret="sec", google_token_encryption_key="k"
    )
    assert s.classroom_enabled is True


@pytest.mark.parametrize(
    ("start", "end"),
    [(4, 4), (5, 4), (-1, 4), (3, 24), (24, 25)],
)
def test_llm_grading_window_must_be_ordered_hours(start: int, end: int) -> None:
    with pytest.raises(ValueError, match="LLM_GRADING_START_HOUR"):
        _settings(llm_grading_start_hour=start, llm_grading_end_hour=end)


def test_llm_grading_window_default_is_valid() -> None:
    s = _settings(llm_grading_start_hour=0, llm_grading_end_hour=23)
    assert (s.llm_grading_start_hour, s.llm_grading_end_hour) == (0, 23)
