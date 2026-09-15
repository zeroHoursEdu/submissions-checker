#!/usr/bin/env python3
"""Generates the alert rules, in both shapes Grafana accepts.

    python3 observability/grafana/build_alerting.py

`rules()` is the provisioning-API body push.py sends to Grafana Cloud; `file_provisioning()`
is the YAML the local Grafana loads from observability/grafana/alerting/. Both come from the
same four definitions, so the laptop shows the rules production alerts on.

Four rules, on purpose. Each one is a failure a teacher would otherwise learn about from a
student; anything finer belongs on the Technical dashboard, not in Telegram.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
OUT = HERE / "alerting" / "alert-rules.yaml"
FOLDER_UID = "subchk"
FOLDER_TITLE = "Submissions Checker"
GROUP = "subchk"
DS_UID = "grafanacloud-prom"
JOB = 'job="submissions-checker"'
LABELS = {"app": "submissions-checker"}

# (uid, title, promql, for, noDataState, summary)
# The threshold step fires on "value > 0". A plain comparison returns the LEFT-HAND VALUE
# when true — `max(app_db_healthy) == 0` yields 0, which never crosses the threshold — and
# an empty vector when false, which Grafana treats as NoData. Comparisons whose true value
# can be 0 therefore use the `bool` modifier so they always yield exactly 0 or 1, and
# NoData then means what it should: no series at all.
_RULES: list[tuple[str, str, str, str, str, str]] = [
    (
        "subchk-service-down",
        "ServiceDown",
        f"max(up{{{JOB}}}) < bool 1",
        "3m",
        "Alerting",  # no scrape at all (Alloy or the host died) IS the outage
        "No replica of Submissions Checker is answering scrapes, or nothing is pushing metrics.",
    ),
    (
        "subchk-high-error-rate",
        "HighErrorRate",
        f'(sum(rate(http_requests_total{{{JOB},status_class="5xx"}}[10m])) '
        f"/ sum(rate(http_requests_total{{{JOB}}}[10m])) > 0.05) "
        f"and (sum(increase(http_requests_total{{{JOB}}}[10m])) > 20)",
        "5m",
        "OK",
        "More than 5% of requests failed with 5xx over the last 10 minutes.",
    ),
    (
        "subchk-db-unhealthy",
        "DbUnhealthy",
        f"max(app_db_healthy{{{JOB}}}) == bool 0",
        "2m",
        "OK",
        "The app cannot query Postgres: the metrics refresh is failing on every replica.",
    ),
    (
        "subchk-outbox-stuck",
        "OutboxStuck",
        f"max(outbox_oldest_pending_age_seconds{{{JOB}}}) > 900",
        "5m",
        "OK",
        "A background job has waited more than 15 minutes; checks and emails are not going out.",
    ),
]


def _data(expr: str) -> list[dict[str, Any]]:
    return [
        {
            "refId": "A",
            "relativeTimeRange": {"from": 600, "to": 0},
            "datasourceUid": DS_UID,
            "model": {
                "refId": "A",
                "expr": expr,
                "instant": True,
                "intervalMs": 1000,
                "maxDataPoints": 43200,
            },
        },
        {
            "refId": "B",
            "datasourceUid": "__expr__",
            "model": {
                "refId": "B",
                "type": "threshold",
                "expression": "A",
                "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
            },
        },
    ]


def _rule(uid: str, title: str, expr: str, for_: str, no_data: str, summary: str) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "condition": "B",
        "data": _data(expr),
        "for": for_,
        "noDataState": no_data,
        "execErrState": "Error",
        "labels": dict(LABELS),
        "annotations": {"summary": summary},
    }


def rules() -> list[dict[str, Any]]:
    """Provisioning-API bodies (POST/PUT /api/v1/provisioning/alert-rules)."""
    return [
        {**_rule(*spec), "folderUID": FOLDER_UID, "ruleGroup": GROUP, "orgID": 1} for spec in _RULES
    ]


def file_provisioning() -> dict[str, Any]:
    """File-provisioning document (/etc/grafana/provisioning/alerting/*.yaml)."""
    return {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": 1,
                "name": GROUP,
                "folder": FOLDER_TITLE,
                "interval": "1m",
                "rules": [_rule(*spec) for spec in _RULES],
            }
        ],
    }


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(yaml.safe_dump(file_provisioning(), sort_keys=False, allow_unicode=True))
    print("wrote", OUT.relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
