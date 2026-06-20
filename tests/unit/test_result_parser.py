"""Unit tests for services.testing.result_parser.

The parser is currently an unimplemented skeleton: both public methods are
documented to raise NotImplementedError. These tests pin that contract so a
future implementation (or accidental removal) is caught.
"""

from __future__ import annotations

import pytest

from submissions_checker.services.testing.result_parser import TestResultParser


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "===== 1 passed in 0.01s =====",
        "garbage not a real report",
        "E   AssertionError\n1 failed",
    ],
)
def test_parse_pytest_output_not_implemented(payload: str) -> None:
    with pytest.raises(NotImplementedError):
        TestResultParser.parse_pytest_output(payload)


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "<testsuite></testsuite>",
        "<not-valid-xml",
        '<testsuite tests="1"><testcase name="t"/></testsuite>',
    ],
)
def test_parse_junit_xml_not_implemented(xml: str) -> None:
    with pytest.raises(NotImplementedError):
        TestResultParser.parse_junit_xml(xml)


def test_methods_are_static() -> None:
    # callable without an instance
    assert callable(TestResultParser.parse_pytest_output)
    assert callable(TestResultParser.parse_junit_xml)
