"""Unit tests for the pure gradebook/integrity rules (services.gradebook).

No database: these are the classification rules a teacher-facing grid and an
integrity table apply to already-fetched rows. DB-touching query functions
(`fetch_roster_rows`, `fetch_integrity_rows`) are covered separately in
tests/functional/test_gradebook.py, matching how services.grading splits
`compute_grade` (unit) from `finalize_grade` (functional).
"""

from __future__ import annotations

from datetime import UTC, datetime

from submissions_checker.db.models.enums import SubmissionStatus
from submissions_checker.services.gradebook import (
    GridSourceRow,
    IntegrityRow,
    RosterRow,
    build_student_grid,
    cell_status,
    compute_cached_stats,
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


# ── compute_cached_stats ─────────────────────────────────────────────────────


def _integrity_row(*, flagged: bool) -> IntegrityRow:
    return IntegrityRow(
        student_id=1,
        student_name="S",
        assignment_id=1,
        assignment_title="A",
        tab_switch=0,
        window_blur=0,
        force_fail=False,
        other_events=0,
        duration_seconds=100,
        median_seconds=100,
        duration_anomalous=False,
        severity="high" if flagged else None,
        flagged=flagged,
    )


def test_compute_cached_stats_average_mark_is_sum_over_sum_as_percent() -> None:
    rows = [
        _row(assignment_id=1, grade=40, max_grade=50),  # 80%
        _row(assignment_id=2, grade=10, max_grade=50),  # 20%
    ]
    stats = compute_cached_stats(rows, [], now=_NOW)
    # (40+10) / (50+50) * 100 = 50.0, NOT the plain mean of 80% and 20%
    assert stats.average_mark_pct == 50.0


def test_compute_cached_stats_average_mark_excludes_ungraded_pairs() -> None:
    rows = [_row(assignment_id=1, grade=40, max_grade=50), _row(assignment_id=2, grade=None)]
    stats = compute_cached_stats(rows, [], now=_NOW)
    assert stats.average_mark_pct == 80.0


def test_compute_cached_stats_average_mark_none_when_nothing_graded() -> None:
    rows = [_row(assignment_id=1, grade=None)]
    assert compute_cached_stats(rows, [], now=_NOW).average_mark_pct is None


def test_compute_cached_stats_pass_pct_is_per_work_not_per_student() -> None:
    rows = [
        _row(student_id=1, assignment_id=1, grade=80, min_grade=50),
        _row(student_id=2, assignment_id=1, grade=30, min_grade=50),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pass_pct == 50.0


def test_compute_cached_stats_pass_pct_counts_ungraded_as_not_passed() -> None:
    rows = [
        _row(student_id=1, assignment_id=1, grade=80, min_grade=50),
        _row(student_id=1, assignment_id=2, grade=None),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pass_pct == 50.0


def test_compute_cached_stats_pass_pct_zero_rows_is_zero() -> None:
    assert compute_cached_stats([], [], now=_NOW).pass_pct == 0.0


def test_compute_cached_stats_pending_review_matches_existing_rule() -> None:
    rows = [
        _row(assignment_id=1, grade=None, submission_status=SubmissionStatus.TESTING),
        _row(assignment_id=2, grade=None, submission_status=SubmissionStatus.COMPLETED),
    ]
    assert compute_cached_stats(rows, [], now=_NOW).pending_review_count == 1


def test_compute_cached_stats_cheating_pct_over_flagged_attempts() -> None:
    integrity = [_integrity_row(flagged=True), _integrity_row(flagged=False)]
    assert compute_cached_stats([], integrity, now=_NOW).cheating_pct == 50.0


def test_compute_cached_stats_cheating_pct_none_when_no_attempts() -> None:
    assert compute_cached_stats([], [], now=_NOW).cheating_pct is None


# ── build_student_grid ───────────────────────────────────────────────────────


def _grid_row(
    *,
    student_id: int = 1,
    student_name: str = "Student 1",
    group_name: str = "IT-21",
    assignment_id: int = 1,
    assignment_title: str = "Lab 1",
    min_grade: int = 0,
    grade: int | None = None,
    submission_status: SubmissionStatus | None = None,
    quiz_score: float | None = None,
    review_score: float | None = None,
) -> GridSourceRow:
    return GridSourceRow(
        student_id=student_id,
        student_name=student_name,
        group_name=group_name,
        assignment_id=assignment_id,
        assignment_title=assignment_title,
        min_grade=min_grade,
        grade=grade,
        submission_status=submission_status,
        quiz_score=quiz_score,
        review_score=review_score,
    )


def test_build_student_grid_columns_in_first_seen_order() -> None:
    rows = [_grid_row(assignment_id=2), _grid_row(assignment_id=1)]
    grid = build_student_grid(rows)
    assert [c.assignment_id for c in grid.columns] == [2, 1]


def test_build_student_grid_cell_carries_quiz_and_review_scores() -> None:
    rows = [_grid_row(grade=90, min_grade=50, quiz_score=88.0, review_score=95.0)]
    grid = build_student_grid(rows)
    cell = grid.rows[0].cells[1]
    assert cell.quiz_score == 88.0
    assert cell.review_score == 95.0
    assert cell.status == "passed"


def test_build_student_grid_total_sums_final_grade_not_sub_marks() -> None:
    rows = [
        _grid_row(assignment_id=1, grade=80, quiz_score=10.0, review_score=10.0),
        _grid_row(assignment_id=2, grade=20, quiz_score=90.0, review_score=90.0),
    ]
    grid = build_student_grid(rows)
    assert grid.rows[0].total == 100


def test_build_student_grid_carries_group_name_and_sorts_by_student_name() -> None:
    rows = [
        _grid_row(student_id=2, student_name="Zed", group_name="IT-22"),
        _grid_row(student_id=1, student_name="Anna", group_name="IT-21"),
    ]
    grid = build_student_grid(rows)
    assert [r.student_name for r in grid.rows] == ["Anna", "Zed"]
    assert grid.rows[0].group_name == "IT-21"
