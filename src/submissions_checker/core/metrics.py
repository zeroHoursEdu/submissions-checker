"""The application's Prometheus registry and every metric it exports.

One module owns every metric so the dashboard and alert generators can be checked against
the real names (tests/unit/test_observability_assets.py). Add a metric here only with a
question it answers; the series budget is under 500 for two replicas on the free tier, which
is shared with another project. No per-student, per-subject or per-question labels.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

# `<name>_created` series double every counter and histogram for no dashboard value.
# Must run before any metric below is constructed.
disable_created_metrics()  # type: ignore[no-untyped-call]

# A private registry: the default one would also carry the GC collector.
REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

CONTENT_TYPE = CONTENT_TYPE_LATEST

# ── Technical ────────────────────────────────────────────────────────────────

app_info = Gauge(
    "app_info", "Build identity; the value is always 1.", ["revision"], registry=REGISTRY
)
app_info.labels(revision=os.environ.get("APP_REVISION") or "unknown").set(1)

http_requests_total = Counter(
    "http_requests",
    "Requests by route template and status class; /metrics and /static are not counted.",
    ["route", "method", "status_class"],
    registry=REGISTRY,
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Request latency, all routes together.",
    buckets=(0.05, 0.25, 1.0, 5.0, 15.0),
    registry=REGISTRY,
)
http_requests_in_progress = Gauge(
    "http_requests_in_progress", "Requests currently being handled.", registry=REGISTRY
)

db_pool_checked_out = Gauge("db_pool_checked_out", "Connections in use.", registry=REGISTRY)
db_pool_size = Gauge(
    "db_pool_size", "Configured connection ceiling (pool + overflow).", registry=REGISTRY
)
app_db_healthy = Gauge(
    "app_db_healthy", "1 when the last metrics refresh could query Postgres.", registry=REGISTRY
)

outbox_pending = Gauge("outbox_pending", "Outbox rows in PENDING.", registry=REGISTRY)
outbox_error = Gauge("outbox_error", "Outbox rows in ERROR.", registry=REGISTRY)
outbox_oldest_pending_age_seconds = Gauge(
    "outbox_oldest_pending_age_seconds",
    "Age of the oldest PENDING row; 0 when none.",
    registry=REGISTRY,
)
outbox_processed_total = Counter(
    "outbox_processed",
    "Outbox messages by event type and outcome.",
    ["event_type", "outcome"],
    registry=REGISTRY,
)

check_duration_seconds = Histogram(
    "check_duration_seconds",
    "Wall time of one sandboxed check.",
    buckets=(5.0, 15.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)
checks_total = Counter("checks", "Sandboxed checks by outcome.", ["outcome"], registry=REGISTRY)
ai_reviews_total = Counter("ai_reviews", "AI reviews by outcome.", ["outcome"], registry=REGISTRY)
notifications_sent_total = Counter(
    "notifications_sent", "Email notifications by outcome.", ["outcome"], registry=REGISTRY
)

# ── Goals ────────────────────────────────────────────────────────────────────

students_total = Gauge("students_total", "Real (non-test) students.", registry=REGISTRY)
students_active = Gauge(
    "students_active",
    "Distinct students who logged in within the window.",
    ["window"],
    registry=REGISTRY,
)
logins_total = Counter("logins", "Successful logins by role.", ["role"], registry=REGISTRY)

quiz_attempts_started_total = Counter(
    "quiz_attempts_started", "Quiz attempts started.", registry=REGISTRY
)
quiz_attempts_finished_total = Counter(
    "quiz_attempts_finished",
    "Quiz attempts reaching a terminal status.",
    ["status"],
    registry=REGISTRY,
)
quiz_attempts_passed_total = Counter(
    "quiz_attempts_passed", "Finished attempts that passed.", registry=REGISTRY
)
quiz_attempts_in_progress = Gauge(
    "quiz_attempts_in_progress", "Attempts IN_PROGRESS now.", registry=REGISTRY
)
quiz_answers_total = Counter("quiz_answers", "Answers recorded.", registry=REGISTRY)

submissions_uploaded_total = Counter(
    "submissions_uploaded", "ZIP submissions accepted.", registry=REGISTRY
)
submissions_awaiting_teacher_review = Gauge(
    "submissions_awaiting_teacher_review", "Teacher review backlog.", registry=REGISTRY
)

disputes_opened_total = Counter("disputes_opened", "Question disputes opened.", registry=REGISTRY)
disputes_resolved_total = Counter(
    "disputes_resolved", "Question disputes resolved by verdict.", ["status"], registry=REGISTRY
)
disputes_open = Gauge("disputes_open", "Disputes in OPEN.", registry=REGISTRY)
air_raid_pauses_total = Counter(
    "air_raid_pauses", "Quiz pauses granted for an air raid.", registry=REGISTRY
)


def registered_sample_names() -> set[str]:
    """Every sample name this registry can expose, whether or not a label set exists yet.

    A labelled metric emits no samples until a label set is touched, so the names are
    derived from each collector's description rather than from a scrape.
    """
    names: set[str] = set()
    for collector in list(REGISTRY._collector_to_names):  # noqa: SLF001 — no public accessor
        describe = getattr(collector, "describe", None)
        for metric in describe() if describe is not None else collector.collect():
            base = metric.name
            if metric.type == "counter":
                names.add(f"{base}_total")
            elif metric.type == "histogram":
                names.update({f"{base}_bucket", f"{base}_count", f"{base}_sum"})
            else:
                names.add(base)
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            names.add(sample.name)
    return names


def render() -> bytes:
    return generate_latest(REGISTRY)
