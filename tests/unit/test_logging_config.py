"""Log format switch: one processor chain, JSON or console, stdlib included."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator

import pytest

from submissions_checker.core.config import Settings, get_settings
from submissions_checker.core.logging import configure_logging, get_logger


@pytest.fixture
def reconfigure(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    get_settings.cache_clear()
    configure_logging()
    # The handler just built writes to capsys' stream, which closes after the test.
    logging.getLogger().handlers[0].setStream(sys.__stdout__)


def _json_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_effective_format_defaults_by_environment() -> None:
    base = {"secret_key": "test-secret-key-minimum-32-chars-long"}
    assert Settings(**base, environment="development").effective_log_format == "console"
    assert Settings(**base, environment="production", debug=False).effective_log_format == "json"
    assert (
        Settings(**base, environment="development", log_format="json").effective_log_format
        == "json"
    )


def test_json_format_renders_structlog_and_stdlib_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reconfigure: None
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    configure_logging()

    get_logger("t.structlog").warning("structlog_line", attempt_id=7)
    logging.getLogger("t.stdlib").warning("stdlib line %s", 1)

    lines = _json_lines(capsys.readouterr().out)
    by_event = {line["event"]: line for line in lines}
    assert by_event["structlog_line"]["attempt_id"] == 7
    assert by_event["structlog_line"]["level"] == "warning"
    assert by_event["stdlib line 1"]["logger"] == "t.stdlib"
    assert by_event["stdlib line 1"]["level"] == "warning"


def test_json_exception_carries_structured_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reconfigure: None
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    configure_logging()

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("t").exception("it_failed")

    line = next(x for x in _json_lines(capsys.readouterr().out) if x["event"] == "it_failed")
    assert line["exception"][0]["exc_type"] == "RuntimeError"


def test_console_format_is_not_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reconfigure: None
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "console")
    get_settings.cache_clear()
    configure_logging()

    get_logger("t").info("console_line", attempt_id=3)

    out = capsys.readouterr().out
    assert "console_line" in out and "attempt_id" in out
    assert not _json_lines(out)


def test_reconfiguring_does_not_duplicate_handlers(reconfigure: None) -> None:
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == 1
