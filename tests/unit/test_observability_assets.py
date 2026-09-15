"""The committed Grafana JSON must be regenerable, self-contained, and reference only
metrics the application actually exports."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from submissions_checker.core import metrics

GRAFANA = Path(__file__).resolve().parents[2] / "observability" / "grafana"
DS = {"type": "prometheus", "uid": "grafanacloud-prom"}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, GRAFANA / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def build() -> ModuleType:
    return _load("build_dashboards")


@pytest.mark.parametrize(
    ("fn", "uid"), [("technical", "subchk-technical"), ("goals", "subchk-goals")]
)
def test_committed_json_matches_generator(build: ModuleType, fn: str, uid: str) -> None:
    generated = getattr(build, fn)()
    committed = json.loads((GRAFANA / f"{uid}.json").read_text())
    assert committed == generated, "run: python3 observability/grafana/build_dashboards.py"


@pytest.mark.parametrize("fn", ["technical", "goals"])
def test_dashboards_are_self_contained(build: ModuleType, fn: str) -> None:
    dash = getattr(build, fn)()
    text = json.dumps(dash)
    # A public/anonymous dashboard resolves no template variables; every query is literal.
    assert "${" not in text and "$env" not in text and "$DS" not in text
    assert dash["uid"].startswith("subchk-") and "subchk" in dash["tags"]
    panels = list(build.iter_panels(dash))
    assert panels
    for panel in panels:
        assert panel["datasource"] == DS, panel["title"]
        assert panel.get("description"), f"panel {panel['title']!r} must say what it answers"
        for target in panel.get("targets", []):
            assert 'job="submissions-checker"' in target["expr"], target["expr"]


@pytest.mark.parametrize("fn", ["technical", "goals"])
def test_dashboards_reference_only_registered_metrics(build: ModuleType, fn: str) -> None:
    referenced = build.metric_names_in(getattr(build, fn)())
    assert referenced, "no metric found in any query"
    unknown = referenced - metrics.registered_sample_names() - {"up"}
    assert not unknown, unknown


def test_every_goal_metric_is_on_a_dashboard(build: ModuleType) -> None:
    """A metric nobody looks at is a series wasted."""
    on_dashboards = build.metric_names_in(build.technical()) | build.metric_names_in(build.goals())
    app_metrics = {
        n
        for n in metrics.registered_sample_names()
        if not n.startswith(("process_", "python_"))
        and not n.endswith(("_count", "_sum"))  # histogram companions
    }
    unused = app_metrics - on_dashboards
    assert not unused, unused


def test_series_budget_estimate_is_under_500(build: ModuleType) -> None:
    # Two replicas, ~75 (route, method) pairs. The estimate lives next to the dashboards so it
    # is revisited whenever a metric is added.
    assert build.estimate_series(replicas=2, route_templates=75) < 500


# ── Alerting ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def alerting() -> ModuleType:
    return _load("build_alerting")


def test_committed_alert_rules_match_generator(alerting: ModuleType) -> None:
    import yaml

    committed = yaml.safe_load((GRAFANA / "alerting" / "alert-rules.yaml").read_text())
    assert committed == alerting.file_provisioning(), (
        "run: python3 observability/grafana/build_alerting.py"
    )


def test_alert_rules_are_the_four_from_the_spec(alerting: ModuleType, build: ModuleType) -> None:
    rules = alerting.rules()
    assert [r["title"] for r in rules] == [
        "ServiceDown",
        "HighErrorRate",
        "DbUnhealthy",
        "OutboxStuck",
    ]
    for rule in rules:
        assert rule["folderUID"] == "subchk" and rule["ruleGroup"] == "subchk"
        assert rule["labels"] == {"app": "submissions-checker"}
        exprs = [d["model"]["expr"] for d in rule["data"] if "expr" in d["model"]]
        assert exprs and all('job="submissions-checker"' in e for e in exprs)
        fake_dash = {"panels": [{"type": "x", "targets": [{"expr": e}]} for e in exprs]}
        referenced = build.metric_names_in(fake_dash)
        assert referenced <= metrics.registered_sample_names() | {"up"}, referenced
    assert rules[0]["noDataState"] == "Alerting"  # no scrape at all IS the outage
    assert all(r["noDataState"] == "OK" for r in rules[1:])


def test_comparisons_that_can_be_true_at_zero_use_bool(alerting: ModuleType) -> None:
    """`max(x) == 0` returns 0 when true, which never crosses a `> 0` threshold; `bool`
    makes it return 1. Found by stopping Postgres locally and watching DbUnhealthy stay
    Normal while ServiceDown fired on NoData."""
    by_title = {r["title"]: r["data"][0]["model"]["expr"] for r in alerting.rules()}
    assert "< bool 1" in by_title["ServiceDown"]
    assert "== bool 0" in by_title["DbUnhealthy"]
