#!/usr/bin/env python3
"""Generates the two committed Grafana dashboards.

    python3 observability/grafana/build_dashboards.py

Writes subchk-technical.json and subchk-goals.json next to this file. Never hand-edit the
JSON: tests/unit/test_observability_assets.py fails when it drifts from this script, and
also when a query names a metric the application does not export.

Every panel description states the question it answers. A panel without one is noise.

Design rules:
- The datasource is the literal uid of the hosted Prometheus; the local harness provisions
  its own Prometheus under the same uid, so one file renders in both places.
- Every query filters on job="submissions-checker" so komora's series, which share the
  same Grafana Cloud stack, never leak in.
- Counters are read with increase()/rate(); replicas restart on every deploy and PromQL
  handles the resets. Gauges are read with max(): both replicas compute the same number.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DS = {"type": "prometheus", "uid": "grafanacloud-prom"}
JOB = 'job="submissions-checker"'
TAGS = ["subchk"]
APP_MEM_LIMIT_BYTES = 320 * 1024 * 1024  # mem_limit in docker-compose.prod.yml

_IDENT_RE = re.compile(r"[a-z][a-z0-9_]*")
_PROMQL_WORDS = {
    "sum", "max", "min", "avg", "count", "rate", "increase", "irate", "histogram_quantile",
    "by", "without", "on", "ignoring", "group_left", "group_right", "and", "or", "unless",
    "le", "instance", "route", "status_class", "outcome", "event_type", "status", "window",
    "role", "revision", "job", "env", "method", "bool", "offset", "abs", "clamp_min",
    "clamp_max", "time", "changes", "max_over_time", "last_over_time", "vector", "scalar",
    "absent", "submissions", "checker", "prod", "local", "d", "h", "m", "s",
}  # fmt: skip


# ── Introspection (used by the tests) ────────────────────────────────────────


def iter_panels(dashboard: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            yield from panel.get("panels", [])
        else:
            yield panel


def metric_names_in(dashboard: dict[str, Any]) -> set[str]:
    """Metric names referenced by any query: identifiers containing an underscore that are
    not PromQL keywords or label names, plus the bare `up`."""
    names: set[str] = set()
    for panel in iter_panels(dashboard):
        for target in panel.get("targets", []):
            # Strip label selectors and their string values first.
            expr = re.sub(r"\{[^}]*\}", "", target["expr"])
            for ident in _IDENT_RE.findall(expr):
                if ident == "up" or ("_" in ident and ident not in _PROMQL_WORDS):
                    names.add(ident)
    return names


def estimate_series(*, replicas: int, route_templates: int) -> int:
    """Rough active-series count, to be revisited whenever a metric is added.

    `route_templates` counts (route, method) pairs; a pair typically shows ~1.5 status
    classes in practice (2xx plus one of 3xx/4xx).
    """
    http_counter = route_templates * 1.5
    http_histogram = 8  # 6 buckets + count + sum
    technical_gauges = 1 + 3 + 3  # in_progress, db_pool_* + app_db_healthy, outbox gauges
    outbox_counter = 12  # ~6 live event types × 2 outcomes
    check_histogram = 8
    outcome_counters = 4 + 2 + 2  # checks, ai_reviews, notifications
    student_metrics = 1 + 3 + 3  # students_total, active windows, logins by role
    quiz_metrics = 1 + 3 + 1 + 1 + 1
    submission_dispute_airraid = 1 + 1 + 1 + 3 + 1 + 1
    process_and_info = 10 + 1
    per_replica = (
        http_counter
        + http_histogram
        + technical_gauges
        + outbox_counter
        + check_histogram
        + outcome_counters
        + student_metrics
        + quiz_metrics
        + submission_dispute_airraid
        + process_and_info
    )
    return int(per_replica * replicas) + replicas  # + up per instance


# ── Panel helpers ────────────────────────────────────────────────────────────


def _target(expr: str, legend: str = "", *, instant: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        t["instant"] = True
        t["range"] = False
    return t


def _targets(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    out = []
    for i, (expr, legend) in enumerate(pairs):
        t = _target(expr, legend)
        t["refId"] = chr(ord("A") + i)
        out.append(t)
    return out


def _thresholds(*steps: tuple[str, float | None]) -> dict[str, Any]:
    return {
        "mode": "absolute",
        "steps": [{"color": color, "value": value} for color, value in steps],
    }


class _Grid:
    """Places panels left-to-right, wrapping at 24 columns; rows reset the cursor."""

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.row_h = 0

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x = 0
            self.y += self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self) -> dict[str, int]:
        if self.x:
            self.y += self.row_h
        self.x, self.row_h = 0, 0
        pos = {"x": 0, "y": self.y, "w": 24, "h": 1}
        self.y += 1
        return pos


def _row(grid: _Grid, title: str) -> dict[str, Any]:
    return {"type": "row", "title": title, "collapsed": False, "gridPos": grid.row(), "panels": []}


def _stat(
    grid: _Grid,
    title: str,
    description: str,
    expr: str,
    *,
    unit: str = "none",
    thresholds: dict[str, Any] | None = None,
    decimals: int | None = None,
    w: int = 4,
    h: int = 4,
    color_mode: str = "value",
    legend: str = "",
    text_mode: str = "value",
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "unit": unit,
        "thresholds": thresholds or _thresholds(("blue", None)),
        # A ratio with an empty denominator is "nothing happened", not an error.
        "noValue": "–",
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid.place(w, h),
        "targets": [_target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": color_mode,
            "graphMode": "none",
            "textMode": text_mode,
            "justifyMode": "center",
        },
    }


def _timeseries(
    grid: _Grid,
    title: str,
    description: str,
    targets: list[dict[str, Any]],
    *,
    unit: str = "short",
    stacked: bool = False,
    bars: bool = False,
    minimum: float | None = 0,
    maximum: float | None = None,
    threshold_line: float | None = None,
    w: int = 8,
    h: int = 7,
) -> dict[str, Any]:
    custom: dict[str, Any] = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 2,
        "fillOpacity": 25 if (stacked or bars) else 8,
        "showPoints": "never",
        "spanNulls": False,
        "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
    }
    defaults: dict[str, Any] = {"unit": unit, "custom": custom}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    if threshold_line is not None:
        custom["thresholdsStyle"] = {"mode": "line"}
        defaults["thresholds"] = _thresholds(("green", None), ("red", threshold_line))
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid.place(w, h),
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def _table(
    grid: _Grid, title: str, description: str, expr: str, *, w: int = 8, h: int = 7
) -> dict[str, Any]:
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid.place(w, h),
        "targets": [
            {**_target(expr, instant=True), "format": "table"},
        ],
        "transformations": [
            {
                "id": "organize",
                "options": {"excludeByName": {"Time": True}, "renameByName": {"Value": "5xx"}},
            }
        ],
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 0}, "overrides": []},
        "options": {"showHeader": True, "sortBy": [{"displayName": "5xx", "desc": True}]},
    }


def _dashboard(
    uid: str, title: str, panels: list[dict[str, Any]], *, refresh: str, time_from: str
) -> dict[str, Any]:
    # Grafana renders nothing for a panel without a unique id.
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    return {
        "uid": uid,
        "title": title,
        "tags": TAGS,
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": refresh,
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": time_from, "to": "now"},
        "templating": {"list": []},
        "annotations": {
            "list": [
                {
                    "name": "Deploys",
                    "datasource": DS,
                    "enable": True,
                    "iconColor": "purple",
                    "expr": f"changes(max by (revision) (app_info{{{JOB}}})[5m:1m]) > 0",
                    "titleFormat": "deploy {{revision}}",
                    "step": "1m",
                }
            ]
        },
        "panels": panels,
    }


# ── Technical ────────────────────────────────────────────────────────────────


def technical() -> dict[str, Any]:
    g = _Grid()
    p: list[dict[str, Any]] = []

    p.append(_row(g, "Service"))
    p.append(
        _stat(
            g,
            "Replicas up",
            "How many app replicas Alloy scraped successfully on the last pass. Production runs "
            "two; one means a rollout or a crashed replica, zero fires ServiceDown.",
            f"sum(up{{{JOB}}})",
            thresholds=_thresholds(("red", None), ("yellow", 1), ("green", 2)),
            color_mode="background",
        )
    )
    p.append(
        _stat(
            g,
            "DB healthy",
            "1 when every replica's last metrics refresh could query Postgres. 0 for two "
            "minutes fires DbUnhealthy.",
            f"min(app_db_healthy{{{JOB}}})",
            thresholds=_thresholds(("red", None), ("green", 1)),
            color_mode="background",
        )
    )
    p.append(
        _stat(
            g,
            "5xx ratio (5m)",
            "Share of requests answered 5xx over the last five minutes. Above 5% for five "
            "minutes fires HighErrorRate.",
            f'(sum(rate(http_requests_total{{{JOB},status_class="5xx"}}[5m])) or vector(0)) '
            f"/ sum(rate(http_requests_total{{{JOB}}}[5m]))",
            unit="percentunit",
            decimals=2,
            thresholds=_thresholds(("green", None), ("yellow", 0.01), ("red", 0.05)),
        )
    )
    p.append(
        _stat(
            g,
            "In progress",
            "Requests being handled right now across replicas. Sustained double digits on a "
            "two-replica host means something upstream is slow.",
            f"sum(http_requests_in_progress{{{JOB}}})",
            thresholds=_thresholds(("green", None), ("yellow", 10), ("red", 30)),
        )
    )
    p.append(
        _stat(
            g,
            "Revision",
            "Commit the running image was built from (APP_REVISION baked in at build). "
            "Answers 'did my fix deploy?'. Deploys are also drawn as annotations.",
            f"max by (revision) (app_info{{{JOB}}})",
            legend="{{revision}}",
            text_mode="name",
            w=8,
        )
    )
    p.append(
        _timeseries(
            g,
            "Requests / min",
            "Traffic. The shape says when students are active; a flat zero during a lesson "
            "says the site is unreachable even if scrapes still succeed.",
            _targets((f"sum(rate(http_requests_total{{{JOB}}}[5m])) * 60", "req/min")),
        )
    )
    p.append(
        _timeseries(
            g,
            "Latency p50 / p95",
            "Request latency across all routes (no per-route histogram: that would cost "
            "hundreds of series). p95 creeping up usually means Postgres or the sandbox.",
            _targets(
                (
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(http_request_duration_seconds_bucket{{{JOB}}}[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(http_request_duration_seconds_bucket{{{JOB}}}[5m])))",
                    "p95",
                ),
            ),
            unit="s",
        )
    )
    p.append(
        _table(
            g,
            "5xx by route (dashboard range)",
            "Which endpoints failed and how often over the selected range. Empty is the "
            "expected state.",
            f'sum by (route) (increase(http_requests_total{{{JOB},status_class="5xx"}}[$__range])) > 0',
        )
    )

    p.append(_row(g, "Database"))
    p.append(
        _timeseries(
            g,
            "Pool connections in use vs ceiling",
            "Checked-out connections per replica against the configured pool+overflow. "
            "Hitting the ceiling means requests queue for a connection; Postgres allows 30 "
            "in total, so the fix is not simply raising it.",
            _targets(
                (f"max by (instance) (db_pool_checked_out{{{JOB}}})", "in use {{instance}}"),
                (f"max(db_pool_size{{{JOB}}})", "ceiling per replica"),
            ),
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "DB healthy per replica",
            "The refresh job's verdict on each replica: 1 = queries succeed. A single replica "
            "at 0 while the other is at 1 points at that container, not at Postgres.",
            _targets((f"app_db_healthy{{{JOB}}}", "{{instance}}")),
            maximum=1,
            w=12,
        )
    )

    p.append(_row(g, "Background work"))
    p.append(
        _stat(
            g,
            "Outbox pending",
            "Rows waiting for the outbox processor. It drains every 10s; a growing number "
            "means the processor is stuck or checks are slower than uploads arrive.",
            f"max(outbox_pending{{{JOB}}})",
            thresholds=_thresholds(("green", None), ("yellow", 10), ("red", 50)),
        )
    )
    p.append(
        _stat(
            g,
            "Outbox error",
            "Rows that failed and are awaiting retry (or exhausted retries). Each is a "
            "student who did not get a check result or an email.",
            f"max(outbox_error{{{JOB}}})",
            thresholds=_thresholds(("green", None), ("yellow", 1), ("red", 5)),
        )
    )
    p.append(
        _stat(
            g,
            "Oldest pending",
            "Age of the oldest PENDING outbox row. Above 15 minutes fires OutboxStuck.",
            f"max(outbox_oldest_pending_age_seconds{{{JOB}}})",
            unit="s",
            thresholds=_thresholds(("green", None), ("yellow", 300), ("red", 900)),
        )
    )
    p.append(
        _stat(
            g,
            "Check p95 (30m)",
            "95th percentile wall time of a sandboxed check over the last 30 minutes. "
            "Sandboxes are capped at 384m/1 CPU; a jump here means a heavier subject image "
            "or a saturated host.",
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(check_duration_seconds_bucket{{{JOB}}}[30m])))",
            unit="s",
            thresholds=_thresholds(("green", None), ("yellow", 60), ("red", 120)),
        )
    )
    p.append(
        _timeseries(
            g,
            "Outbox processed / h by outcome",
            "Background messages finished vs errored per hour. Errors here are the first "
            "sign of a broken sandbox image, AI key or email provider.",
            _targets(
                (f"sum by (outcome) (increase(outbox_processed_total{{{JOB}}}[1h]))", "{{outcome}}")
            ),
            stacked=True,
        )
    )
    p.append(
        _timeseries(
            g,
            "Checks by outcome (range)",
            "Sandboxed checks over the selected range: passed, failed (tests), "
            "validation_failed (bad ZIP/config) and error (sandbox crashed). Only 'error' is "
            "the platform's fault.",
            _targets(
                (f"sum by (outcome) (increase(checks_total{{{JOB}}}[$__range]))", "{{outcome}}")
            ),
            bars=True,
            stacked=True,
        )
    )
    p.append(
        _timeseries(
            g,
            "AI reviews by outcome (range)",
            "Reviews returned by the AI provider vs failed (provider error or malformed "
            "verdict). A run of errors means the API key or model name is wrong.",
            _targets(
                (
                    f"sum by (outcome) (increase(ai_reviews_total{{{JOB}}}[$__range]))",
                    "{{outcome}}",
                )
            ),
            bars=True,
            stacked=True,
        )
    )
    p.append(
        _timeseries(
            g,
            "Emails by outcome (range)",
            "Notification emails sent vs failed over the selected range. Failures mean the "
            "provider is rejecting us (unverified sender, quota) and students hear nothing.",
            _targets(
                (
                    f"sum by (outcome) (increase(notifications_sent_total{{{JOB}}}[$__range]))",
                    "{{outcome}}",
                )
            ),
            bars=True,
            stacked=True,
        )
    )
    p.append(
        _timeseries(
            g,
            "Check duration p50 / p95",
            "Sandboxed check wall time over time. Compare against the histogram buckets "
            "(5s to 120s): a p95 above the last bucket is invisible, so raise the buckets "
            "if that happens.",
            _targets(
                (
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(check_duration_seconds_bucket{{{JOB}}}[30m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(check_duration_seconds_bucket{{{JOB}}}[30m])))",
                    "p95",
                ),
            ),
            unit="s",
        )
    )

    p.append(_row(g, "Process"))
    p.append(
        _timeseries(
            g,
            "Resident memory per replica",
            f"RSS of each app process. The red line is the container's mem_limit "
            f"({APP_MEM_LIMIT_BYTES // (1024 * 1024)}m); crossing it is an OOM kill, which shows "
            "up as a replica restart and a counter reset.",
            _targets((f"process_resident_memory_bytes{{{JOB}}}", "{{instance}}")),
            unit="bytes",
            threshold_line=APP_MEM_LIMIT_BYTES,
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "CPU per replica",
            "CPU seconds per second of each app process (1.0 = one core). The host has two "
            "cores shared with Postgres, MinIO and every sandbox.",
            _targets((f"rate(process_cpu_seconds_total{{{JOB}}}[5m])", "{{instance}}")),
            unit="percentunit",
            w=12,
        )
    )

    return _dashboard(
        "subchk-technical",
        "Submissions Checker — Technical",
        p,
        refresh="1m",
        time_from="now-6h",
    )


# ── Goals ────────────────────────────────────────────────────────────────────


def goals() -> dict[str, Any]:
    g = _Grid()
    p: list[dict[str, Any]] = []

    p.append(_row(g, "Students"))
    p.append(
        _stat(
            g,
            "Students",
            "Real students with an account (test entities excluded). The denominator for "
            "every adoption question below.",
            f"max(students_total{{{JOB}}})",
        )
    )
    for window, label in (("1d", "today"), ("7d", "this week"), ("30d", "this month")):
        p.append(
            _stat(
                g,
                f"Active {label}",
                f"Distinct students who logged in during the last {window}. Divided by "
                "Students, this is the share of the roster actually using the service.",
                f'max(students_active{{{JOB},window="{window}"}})',
            )
        )
    p.append(
        _stat(
            g,
            "Taking a quiz now",
            "Quiz attempts currently IN_PROGRESS. Live view of a lesson; also the number of "
            "people a restart right now would interrupt.",
            f"max(quiz_attempts_in_progress{{{JOB}}})",
            thresholds=_thresholds(("blue", None), ("orange", 1)),
            color_mode="background",
        )
    )
    p.append(
        _stat(
            g,
            "Awaiting teacher",
            "Submissions waiting for a teacher's review. A teacher backlog, not a system "
            "one — but students are waiting either way.",
            f"max(submissions_awaiting_teacher_review{{{JOB}}})",
            thresholds=_thresholds(("green", None), ("yellow", 5), ("red", 20)),
        )
    )
    p.append(
        _timeseries(
            g,
            "Logins per day by role",
            "Successful logins per day. Student logins are the usage pulse; teacher logins "
            "say whether the review side keeps up.",
            _targets((f"sum by (role) (increase(logins_total{{{JOB}}}[1d]))", "{{role}}")),
            bars=True,
            stacked=True,
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "Active students, trend",
            "The three active-student windows over time. A widening gap between 30d and 7d "
            "means students tried the service once and did not come back.",
            _targets(
                (f'max(students_active{{{JOB},window="1d"}})', "1d"),
                (f'max(students_active{{{JOB},window="7d"}})', "7d"),
                (f'max(students_active{{{JOB},window="30d"}})', "30d"),
            ),
            w=12,
        )
    )

    p.append(_row(g, "Quizzes"))
    p.append(
        _stat(
            g,
            "Pass rate (range)",
            "Passed attempts over completed attempts in the selected range. Timed-out and "
            "violation-failed attempts are excluded: they say nothing about the questions.",
            f"sum(increase(quiz_attempts_passed_total{{{JOB}}}[$__range])) / "
            f'sum(increase(quiz_attempts_finished_total{{{JOB},status="COMPLETED"}}[$__range]))',
            unit="percentunit",
            decimals=0,
            thresholds=_thresholds(("red", None), ("yellow", 0.5), ("green", 0.7)),
        )
    )
    p.append(
        _stat(
            g,
            "Attempts (range)",
            "Quiz attempts started in the selected range.",
            f"sum(increase(quiz_attempts_started_total{{{JOB}}}[$__range]))",
            decimals=0,
        )
    )
    p.append(
        _stat(
            g,
            "Answers / min",
            "Answers recorded per minute right now. The finest-grained 'is anyone using it "
            "this second' signal there is.",
            f"sum(rate(quiz_answers_total{{{JOB}}}[5m])) * 60",
            decimals=1,
        )
    )
    p.append(
        _timeseries(
            g,
            "Attempts started / finished per day",
            "Started vs finished per day. Started consistently above finished means students "
            "abandon attempts — check the time limit and the finish-status mix.",
            _targets(
                (f"sum(increase(quiz_attempts_started_total{{{JOB}}}[1d]))", "started"),
                (f"sum(increase(quiz_attempts_finished_total{{{JOB}}}[1d]))", "finished"),
            ),
            bars=True,
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "Finish status mix per day",
            "How attempts end: COMPLETED, TIMED_OUT or VIOLATION_FAIL (anti-cheat). A rise "
            "in violations after a config change usually means the rules are too strict.",
            _targets(
                (
                    f"sum by (status) (increase(quiz_attempts_finished_total{{{JOB}}}[1d]))",
                    "{{status}}",
                )
            ),
            bars=True,
            stacked=True,
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "Answers per hour",
            "Quiz answers recorded per hour. Shows when lessons happen; a plateau during a "
            "lesson can mean the timer or the pause button is confusing students.",
            _targets((f"sum(increase(quiz_answers_total{{{JOB}}}[1h]))", "answers")),
            w=24,
            h=6,
        )
    )

    p.append(_row(g, "Submissions"))
    p.append(
        _timeseries(
            g,
            "Uploads per day",
            "ZIP submissions accepted per day. Assignment usage, as opposed to quiz usage.",
            _targets((f"sum(increase(submissions_uploaded_total{{{JOB}}}[1d]))", "uploads")),
            bars=True,
            w=12,
        )
    )
    p.append(
        _timeseries(
            g,
            "Teacher review backlog",
            "Submissions awaiting a teacher over time. Should saw-tooth: it climbs during a "
            "lesson and drops when the teacher reviews. A line that only climbs is a "
            "teacher who has stopped reviewing.",
            _targets((f"max(submissions_awaiting_teacher_review{{{JOB}}})", "awaiting review")),
            w=12,
        )
    )

    p.append(_row(g, "Support"))
    p.append(
        _stat(
            g,
            "Open disputes",
            "Question disputes waiting for a teacher's ruling.",
            f"max(disputes_open{{{JOB}}})",
            thresholds=_thresholds(("green", None), ("yellow", 1), ("red", 5)),
        )
    )
    p.append(
        _stat(
            g,
            "Air-raid pauses (range)",
            "Quiz pauses granted because an air-raid alert was active over the student. Says "
            "whether the feature is used at all, and how often lessons are interrupted.",
            f"sum(increase(air_raid_pauses_total{{{JOB}}}[$__range]))",
            decimals=0,
        )
    )
    p.append(
        _timeseries(
            g,
            "Disputes opened / resolved per day",
            "Disputes filed vs rulings made, by verdict. Many ACCEPTED rulings on one quiz "
            "means a broken answer key; many REJECTED means students game the button.",
            _targets(
                (f"sum(increase(disputes_opened_total{{{JOB}}}[1d]))", "opened"),
                (
                    f"sum by (status) (increase(disputes_resolved_total{{{JOB}}}[1d]))",
                    "resolved {{status}}",
                ),
            ),
            bars=True,
            w=16,
        )
    )

    return _dashboard(
        "subchk-goals", "Submissions Checker — Goals", p, refresh="5m", time_from="now-30d"
    )


def main() -> None:
    for build in (technical, goals):
        dash = build()
        path = HERE / f"{dash['uid']}.json"
        path.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
        print("wrote", path.relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
