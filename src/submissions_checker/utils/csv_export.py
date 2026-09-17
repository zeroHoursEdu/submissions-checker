"""Neutralise spreadsheet formula injection in CSV exports.

A cell beginning with ``=``, ``+``, ``-`` or ``@`` (or a tab / carriage return that
hides one) is evaluated as a formula by Excel and LibreOffice when the file is opened.
Student-written text (feedback, names) ends up in the teacher's spreadsheet, so every
string cell is prefixed with an apostrophe when it starts with one of those characters.
The apostrophe is the spreadsheet convention for "this is text" and is not displayed.
"""

from __future__ import annotations

from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """Return ``value`` with a leading apostrophe when a spreadsheet would evaluate it."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
