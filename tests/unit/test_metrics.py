"""The private Prometheus registry: what is and is not exported."""

from __future__ import annotations

from submissions_checker.core import metrics


def test_registry_has_no_gc_or_created_series() -> None:
    names = metrics.registered_sample_names()
    assert not any(n.startswith("python_gc_") for n in names), "GC collector is noise"
    assert not any(n.endswith("_created") for n in names), "_created series double the count"


def test_registry_exports_process_and_app_info() -> None:
    names = metrics.registered_sample_names()
    assert "process_resident_memory_bytes" in names
    assert "app_info" in names


def test_catalogue_names_are_registered() -> None:
    names = metrics.registered_sample_names()
    expected = {
        "http_requests_total",
        "http_request_duration_seconds_bucket",
        "http_requests_in_progress",
        "db_pool_checked_out",
        "db_pool_size",
        "app_db_healthy",
        "outbox_pending",
        "outbox_error",
        "outbox_oldest_pending_age_seconds",
        "outbox_processed_total",
        "check_duration_seconds_bucket",
        "checks_total",
        "ai_reviews_total",
        "notifications_sent_total",
        "students_total",
        "students_active",
        "logins_total",
        "quiz_attempts_started_total",
        "quiz_attempts_finished_total",
        "quiz_attempts_in_progress",
        "quiz_answers_total",
        "quiz_attempts_passed_total",
        "submissions_uploaded_total",
        "submissions_awaiting_teacher_review",
        "disputes_opened_total",
        "disputes_resolved_total",
        "disputes_open",
        "air_raid_pauses_total",
    }
    missing = expected - names
    assert not missing, missing


def test_histogram_buckets_are_few() -> None:
    # 5 explicit buckets + +Inf. More buckets is more series for no dashboard gain.
    assert metrics.http_request_duration_seconds._upper_bounds[-1] == float("inf")
    assert len(metrics.http_request_duration_seconds._upper_bounds) == 6
    assert len(metrics.check_duration_seconds._upper_bounds) == 6


def test_render_is_text_exposition() -> None:
    body = metrics.render().decode()
    assert "# HELP http_requests_total" in body
    assert metrics.CONTENT_TYPE.startswith("text/plain")
