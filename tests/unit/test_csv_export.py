"""Cells that a spreadsheet would evaluate are neutralised before they leave the app."""

from __future__ import annotations

import pytest

from submissions_checker.utils.csv_export import csv_safe


@pytest.mark.parametrize(
    "raw",
    ['=HYPERLINK("http://x")', "+1+1", "-2", "@SUM(A1)", "\t=1", "\r=1", "=cmd|' /C calc'!A0"],
)
def test_formula_prefixes_are_escaped(raw: str) -> None:
    assert csv_safe(raw) == "'" + raw


@pytest.mark.parametrize("raw", ["Ada Lovelace", "ada@example.com", "", "1 - 2", "a=b"])
def test_plain_text_is_untouched(raw: str) -> None:
    assert csv_safe(raw) == raw


def test_non_strings_pass_through() -> None:
    assert csv_safe(5) == 5
    assert csv_safe(None) is None
    assert csv_safe(2.5) == 2.5
