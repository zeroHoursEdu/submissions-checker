"""Structured logging configuration using structlog."""

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

from submissions_checker.core.config import get_settings


def add_app_context(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Add application context to log entries."""
    settings = get_settings()
    event_dict["environment"] = settings.environment
    return event_dict


def configure_logging() -> None:
    """Configure structlog and route stdlib/uvicorn records through the same chain.

    One processor chain, two renderers: every line on stdout is either a JSON object (what
    Alloy ships to Loki) or a console line, never a mix. Safe to call more than once.
    """
    settings = get_settings()
    log_format = settings.effective_log_format

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        add_app_context,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Cached loggers ignore later reconfiguration, which breaks capture_logs in tests.
        cache_logger_on_first_use=settings.is_production,
    )

    final_processors: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    # Tracebacks never render frame locals: a failing SEND_CREDENTIALS or /auth/login frame
    # holds passwords, tokens and emails. structlog's `dict_tracebacks` shows them by
    # default, and the rich console formatter would too if rich were ever installed.
    if log_format == "json":
        final_processors += [
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            structlog.processors.JSONRenderer(),
        ]
    else:
        final_processors.append(
            structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors, processors=final_processors
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, settings.log_level.upper()))

    # uvicorn installs its own plain-text handlers before importing the app; hand its
    # records to the root handler instead so they are rendered like everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    # Replaced by the `http_request` line from RequestLoggingMiddleware.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a logger instance."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
