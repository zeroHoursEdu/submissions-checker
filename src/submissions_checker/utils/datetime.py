"""Datetime utilities."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """
    Get current UTC datetime with timezone awareness.

    Returns:
        Current UTC datetime
    """
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """
    Convert datetime to UTC timezone.

    Args:
        dt: Datetime to convert

    Returns:
        Datetime in UTC timezone
    """
    if dt.tzinfo is None:
        # Assume naive datetime is UTC
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_iso(dt: datetime) -> str:
    """
    Format datetime as ISO 8601 string.

    Args:
        dt: Datetime to format

    Returns:
        ISO 8601 formatted string
    """
    return dt.isoformat()


def parse_iso(iso_string: str) -> datetime:
    """
    Parse ISO 8601 datetime string.

    Args:
        iso_string: ISO 8601 formatted datetime string

    Returns:
        Parsed datetime
    """
    return datetime.fromisoformat(iso_string)
