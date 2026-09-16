"""Unit tests for the pure gradebook/integrity rules (services.gradebook).

No database: these are the classification rules a teacher-facing grid and an
integrity table apply to already-fetched rows. DB-touching query functions
(`fetch_roster_rows`, `fetch_integrity_rows`) are covered separately in
tests/functional/test_gradebook.py, matching how services.grading splits
`compute_grade` (unit) from `finalize_grade` (functional).
"""

from __future__ import annotations

from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services.gradebook import (
    cell_status,
    duration_anomalous,
    median_duration,
    severity_for,
)


def test_cell_status_graded_at_or_above_min_is_passed() -> None:
    assert cell_status(grade=50, min_grade=50, submission_status=None) == "passed"


def test_cell_status_graded_below_min_is_failed() -> None:
    assert cell_status(grade=40, min_grade=50, submission_status=None) == "failed"


def test_cell_status_ungraded_with_pending_submission_is_pending() -> None:
    status = cell_status(
        grade=None, min_grade=0, submission_status=SubmissionStatus.AWAITING_TEACHER_REVIEW
    )
    assert status == "pending"


def test_cell_status_ungraded_with_terminal_submission_is_not_submitted() -> None:
    # A FAILED submission with no grade set is not "pending" — the pipeline is done.
    status = cell_status(grade=None, min_grade=0, submission_status=SubmissionStatus.FAILED)
    assert status == "not_submitted"


def test_cell_status_no_submission_at_all_is_not_submitted() -> None:
    assert cell_status(grade=None, min_grade=0, submission_status=None) == "not_submitted"


def test_severity_force_fail_is_high_regardless_of_counts() -> None:
    result = severity_for(force_fail=True, tab_switch=0, window_blur=0)
    assert result == "high"


def test_severity_three_combined_violations_is_high() -> None:
    result = severity_for(force_fail=False, tab_switch=2, window_blur=1)
    assert result == "high"


def test_severity_two_combined_violations_is_medium() -> None:
    result = severity_for(force_fail=False, tab_switch=1, window_blur=1)
    assert result == "medium"


def test_severity_one_violation_is_medium() -> None:
    result = severity_for(force_fail=False, tab_switch=1, window_blur=0)
    assert result == "medium"


def test_severity_zero_violations_is_none() -> None:
    assert severity_for(force_fail=False, tab_switch=0, window_blur=0) is None


def test_duration_anomalous_under_30_percent_of_median() -> None:
    assert duration_anomalous(duration_seconds=50, median_seconds=200) is True


def test_duration_anomalous_at_exactly_30_percent_is_not_anomalous() -> None:
    assert duration_anomalous(duration_seconds=60, median_seconds=200) is False


def test_duration_anomalous_missing_median_is_false() -> None:
    assert duration_anomalous(duration_seconds=10, median_seconds=None) is False


def test_duration_anomalous_missing_duration_is_false() -> None:
    assert duration_anomalous(duration_seconds=None, median_seconds=200) is False


def test_median_duration_odd_count() -> None:
    assert median_duration([100, 300, 200]) == 200


def test_median_duration_even_count_rounds() -> None:
    # median of [100, 201] is 150.5 -> rounds to 150 (banker's rounding on .5 is fine here)
    assert median_duration([100, 201]) == 150 or median_duration([100, 201]) == 151


def test_median_duration_empty_list_is_none() -> None:
    assert median_duration([]) is None
