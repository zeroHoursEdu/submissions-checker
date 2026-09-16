"""Unit tests for the pure gradebook/integrity rules (services.gradebook).

No database: these are the classification rules a teacher-facing grid and an
integrity table apply to already-fetched rows. DB-touching query functions
(`fetch_roster_rows`, `fetch_integrity_rows`) are covered separately in
tests/functional/test_gradebook.py, matching how services.grading splits
`compute_grade` (unit) from `finalize_grade` (functional).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services.gradebook import (
    RosterRow,
    build_grid,
    cell_status,
    compute_stats,
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


_NOW = datetime(2026, 9, 16, tzinfo=UTC)


def _row(
    *,
    student_id: int = 1,
    assignment_id: int = 1,
    grade: int | None = None,
    deadline: datetime | None = None,
    submission_status: SubmissionStatus | None = None,
    min_grade: int = 0,
    max_grade: int = 100,
) -> RosterRow:
    return RosterRow(
        student_id=student_id,
        student_name=f"Student {student_id}",
        assignment_id=assignment_id,
        assignment_code=f"a{assignment_id}",
        assignment_title=f"Assignment {assignment_id}",
        min_grade=min_grade,
        max_grade=max_grade,
        deadline=deadline,
        student_assignment_id=student_id * 100 + assignment_id,
        grade=grade,
        submission_status=submission_status,
    )


def test_compute_stats_average_score_over_graded_only() -> None:
    rows = [_row(grade=80), _row(grade=None), _row(grade=60, assignment_id=2)]
    stats = compute_stats(rows, now=_NOW)
    assert stats.average_score == 70.0


def test_compute_stats_average_score_none_when_nothing_graded() -> None:
    rows = [_row(grade=None)]
    assert compute_stats(rows, now=_NOW).average_score is None


def test_compute_stats_pass_rate_requires_every_assignment_graded() -> None:
    # Student 1 graded on both assignments -> passed. Student 2 graded on only one -> not.
    rows = [
        _row(student_id=1, assignment_id=1, grade=50),
        _row(student_id=1, assignment_id=2, grade=50),
        _row(student_id=2, assignment_id=1, grade=50),
        _row(student_id=2, assignment_id=2, grade=None),
    ]
    stats = compute_stats(rows, now=_NOW)
    assert stats.pass_rate_pct == 50.0


def test_compute_stats_pass_rate_zero_assignments_is_zero() -> None:
    assert compute_stats([], now=_NOW).pass_rate_pct == 0.0


def test_compute_stats_overdue_counts_ungraded_past_deadline_including_never_submitted() -> None:
    past = _NOW - timedelta(days=1)
    future = _NOW + timedelta(days=1)
    rows = [
        _row(assignment_id=1, grade=None, deadline=past, submission_status=None),
        _row(assignment_id=2, grade=None, deadline=future, submission_status=None),
        _row(assignment_id=3, grade=50, deadline=past),
        _row(assignment_id=4, grade=None, deadline=None),
    ]
    assert compute_stats(rows, now=_NOW).overdue_count == 1


def test_compute_stats_pending_review_counts_ungraded_non_terminal_submissions() -> None:
    rows = [
        _row(assignment_id=1, grade=None, submission_status=SubmissionStatus.TESTING),
        _row(assignment_id=2, grade=None, submission_status=SubmissionStatus.COMPLETED),
        _row(assignment_id=3, grade=None, submission_status=None),
        _row(assignment_id=4, grade=None, submission_status=SubmissionStatus.AWAITING_TEACHER_REVIEW),
    ]
    assert compute_stats(rows, now=_NOW).pending_review_count == 2


def test_build_grid_columns_follow_first_seen_row_order() -> None:
    rows = [
        _row(student_id=1, assignment_id=2, grade=None),
        _row(student_id=1, assignment_id=1, grade=None),
    ]
    grid = build_grid(rows)
    assert [c.assignment_id for c in grid.columns] == [2, 1]


def test_build_grid_row_total_sums_graded_cells_only() -> None:
    rows = [
        _row(student_id=1, assignment_id=1, grade=80),
        _row(student_id=1, assignment_id=2, grade=None),
        _row(student_id=1, assignment_id=3, grade=20),
    ]
    grid = build_grid(rows)
    assert grid.rows[0].total == 100


def test_build_grid_row_total_is_none_when_nothing_graded() -> None:
    rows = [_row(student_id=1, assignment_id=1, grade=None)]
    grid = build_grid(rows)
    assert grid.rows[0].total is None


def test_build_grid_cell_status_matches_cell_status_rule() -> None:
    rows = [_row(student_id=1, assignment_id=1, grade=90, min_grade=50)]
    grid = build_grid(rows)
    assert grid.rows[0].cells[1].status == "passed"
    assert grid.rows[0].cells[1].grade == 90


def test_build_grid_rows_sorted_by_student_name() -> None:
    rows = [
        _row(student_id=2, assignment_id=1),
        _row(student_id=1, assignment_id=1),
    ]
    grid = build_grid(rows)
    # _row() names students "Student {id}" so lexical order is Student 1, Student 2
    assert [r.student_id for r in grid.rows] == [1, 2]
