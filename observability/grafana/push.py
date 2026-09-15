#!/usr/bin/env python3
"""Push the committed dashboards and alerting to Grafana Cloud.

    python3 observability/grafana/push.py dashboards   # folder + two dashboards
    python3 observability/grafana/push.py alerting     # contact point + policy route + rules
    python3 observability/grafana/push.py all

Reads GRAFANA_URL, GRAFANA_API_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID from the
environment (the Makefile sources .env). Touches only the folder `subchk`, the contact point
`subchk-telegram`, one notification-policy route matching app=submissions-checker, and the
rule group `subchk`. It never lists, edits or deletes anything else: another project's
dashboards live on the same stack.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
FOLDER_UID, FOLDER_TITLE = "subchk", "Submissions Checker"
CONTACT_POINT = "subchk-telegram"
DASHBOARDS = ("subchk-technical", "subchk-goals")


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is empty — set it in .env (see .env.example for where it comes from)")
    return value


def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    req = urllib.request.Request(
        _env("GRAFANA_URL").rstrip("/") + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_env('GRAFANA_API_TOKEN')}",
            "Content-Type": "application/json",
            # Pushed objects stay editable in the UI, so thresholds can be tuned there.
            "X-Disable-Provenance": "true",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — https URL from .env
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return err.code, raw.decode(errors="replace")


def ensure_folder() -> None:
    status, existing = _call("GET", f"/api/folders/{FOLDER_UID}")
    if status == 200:
        if existing["title"] != FOLDER_TITLE:
            sys.exit(
                f"folder uid {FOLDER_UID} exists with title {existing['title']!r}; refusing to touch it"
            )
        return
    status, created = _call("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
    if status not in (200, 201):
        sys.exit(f"could not create folder: {status} {created}")
    print("created folder", FOLDER_TITLE)


def push_dashboards() -> None:
    ensure_folder()
    for name in DASHBOARDS:
        dash = json.loads((HERE / f"{name}.json").read_text())
        dash["id"] = None
        status, res = _call(
            "POST",
            "/api/dashboards/db",
            {"dashboard": dash, "folderUid": FOLDER_UID, "overwrite": True, "message": "push.py"},
        )
        if status != 200:
            sys.exit(f"dashboard {name}: {status} {res}")
        print("pushed", res["url"])


def push_alerting() -> None:
    ensure_folder()
    sys.path.insert(0, str(HERE))
    from build_alerting import GROUP, rules  # noqa: PLC0415

    contact_point = {
        "name": CONTACT_POINT,
        "type": "telegram",
        "settings": {
            "bottoken": _env("TELEGRAM_BOT_TOKEN"),
            "chatid": _env("TELEGRAM_CHAT_ID"),
            "parse_mode": "HTML",
        },
        "disableResolveMessage": False,
    }
    status, existing = _call("GET", f"/api/v1/provisioning/contact-points?name={CONTACT_POINT}")
    if status == 200 and existing:
        uid = existing[0]["uid"]
        status, res = _call(
            "PUT", f"/api/v1/provisioning/contact-points/{uid}", {**contact_point, "uid": uid}
        )
    else:
        status, res = _call("POST", "/api/v1/provisioning/contact-points", contact_point)
    if status not in (200, 201, 202):
        sys.exit(f"contact point: {status} {res}")
    print("contact point", CONTACT_POINT, "ok")

    # Read-modify-write the policy tree: keep every existing route, replace only ours.
    status, policy = _call("GET", "/api/v1/provisioning/policies")
    if status != 200:
        sys.exit(f"read policy tree: {status} {policy}")
    routes = [r for r in policy.get("routes") or [] if r.get("receiver") != CONTACT_POINT]
    routes.append(
        {
            "receiver": CONTACT_POINT,
            "object_matchers": [["app", "=", "submissions-checker"]],
            "group_by": ["alertname"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        }
    )
    policy["routes"] = routes
    status, res = _call("PUT", "/api/v1/provisioning/policies", policy)
    if status not in (200, 202):
        sys.exit(f"policy: {status} {res}")
    print("policy route ok")

    for rule in rules():
        status, res = _call("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule)
        if status == 404:
            status, res = _call("POST", "/api/v1/provisioning/alert-rules", rule)
        if status not in (200, 201):
            sys.exit(f"rule {rule['title']}: {status} {res}")
        print("rule", rule["title"], "ok")

    status, res = _call(
        "PUT",
        f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{GROUP}",
        {"title": GROUP, "folderUid": FOLDER_UID, "interval": 60, "rules": rules()},
    )
    if status not in (200, 202):
        # The rules already exist; only the group interval failed to apply.
        print(f"warning: could not set group interval ({status} {res}); it defaults to 1m")
    else:
        print("rule group", GROUP, "interval 1m")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what not in ("dashboards", "alerting", "all"):
        sys.exit(__doc__)
    if what in ("dashboards", "all"):
        push_dashboards()
    if what in ("alerting", "all"):
        push_alerting()
