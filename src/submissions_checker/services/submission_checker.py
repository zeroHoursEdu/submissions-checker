"""Submission validity checker — currently a stub; real implementation TBD."""

from pathlib import Path


def check_submission(zip_path: Path) -> tuple[bool, str]:
    """Check whether a submitted ZIP passes validation.

    Returns (passed, reason). When passed is True reason is empty.
    When passed is False, reason is shown to the student.
    """
    # TODO: unzip, run tests, verify structure, etc.
    return True, ""
