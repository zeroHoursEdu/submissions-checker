#!/usr/bin/env python3
"""Generates the alert rules, in both shapes Grafana accepts.

    python3 observability/grafana/build_alerting.py

`rules()` is the provisioning-API body push.py sends to Grafana Cloud; `file_provisioning()`
is the YAML the local Grafana loads from observability/grafana/alerting/. Both come from the
same definitions, so the laptop shows the rules production alerts on.

Six rules, on purpose. Each one is a failure a teacher would otherwise learn about from a
student; anything finer belongs on the Technical or Logs dashboard, not in Telegram. Four
read Prometheus; the last two read Loki and fire only on production logs (`env="prod"`), so
a laptop run of the app can never page anyone.
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

LOGS_DS_UID = "grafanacloud-logs"

# Log rules read production only: a laptop run must never page.
_PROD_APP = '{app="subchk", env="prod", service="app"}'
_LOG_RULES: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "subchk-quiz-force-fail-spike",
        "QuizForceFailSpike",
        f'sum(count_over_time({_PROD_APP} | json | event="quiz_attempt_force_failed" [10m])) > 5',
        "0m",
        "OK",
        "More than 5 quiz attempts auto-failed in 10 minutes: a rule or a browser change is likely misfiring. Open the Quiz investigation dashboard.",
        600,
    ),
    (
        "subchk-outbox-dead",
        "OutboxDead",
        f'sum(count_over_time({_PROD_APP} | json | event="outbox_dead" [15m])) > 0',
        "0m",
        "OK",
        "A background job exhausted its retries (check, AI review or email lost). See the Logs dashboard.",
        900,
    ),
]


def _data(expr: str, *, ds_uid: str = DS_UID, range_s: int = 600) -> list[dict[str, Any]]:
    return [
        {
            "refId": "A",
            "relativeTimeRange": {"from": range_s, "to": 0},
            "datasourceUid": ds_uid,
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


def _rule(
    uid: str,
    title: str,
    expr: str,
    for_: str,
    no_data: str,
    summary: str,
    *,
    ds_uid: str = DS_UID,
    range_s: int = 600,
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "condition": "B",
        "data": _data(expr, ds_uid=ds_uid, range_s=range_s),
        "for": for_,
        "noDataState": no_data,
        "execErrState": "Error",
        "labels": dict(LABELS),
        "annotations": {"summary": summary},
    }


def rules() -> list[dict[str, Any]]:
    """Provisioning-API bodies (POST/PUT /api/v1/provisioning/alert-rules)."""
    all_specs = [_rule(*spec) for spec in _RULES] + [
        _rule(*spec[:6], ds_uid=LOGS_DS_UID, range_s=spec[6]) for spec in _LOG_RULES
    ]
    return [{**spec, "folderUID": FOLDER_UID, "ruleGroup": GROUP, "orgID": 1} for spec in all_specs]


def file_provisioning() -> dict[str, Any]:
    """File-provisioning document (/etc/grafana/provisioning/alerting/*.yaml)."""
    all_rules = [_rule(*spec) for spec in _RULES] + [
        _rule(*spec[:6], ds_uid=LOGS_DS_UID, range_s=spec[6]) for spec in _LOG_RULES
    ]
    return {
        "apiVersion": 1,
        "groups": [
            {
                "orgId": 1,
                "name": GROUP,
                "folder": FOLDER_TITLE,
                "interval": "1m",
                "rules": all_rules,
            }
        ],
    }


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(yaml.safe_dump(file_provisioning(), sort_keys=False, allow_unicode=True))
    print("wrote", OUT.relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
