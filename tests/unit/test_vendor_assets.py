"""No page depends on a third-party host at runtime: every script and stylesheet is ours."""

from __future__ import annotations

import re
from pathlib import Path

from scripts import fetch_vendor_assets as vendor
from submissions_checker.core.security_headers import CONTENT_SECURITY_POLICY

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def test_templates_load_no_external_scripts_or_styles() -> None:
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'<(script|link)[^>]+(src|href)="(https?:)?//[^"]+"', text):
            offenders.append(f"{path.name}: {m.group(0)[:80]}")
    assert offenders == []


def test_tailwind_is_a_pinned_vendor_asset() -> None:
    assert "tailwind/tailwind.js" in vendor.ASSETS
    assert re.search(r"cdn\.tailwindcss\.com/\d+\.\d+\.\d+$", vendor.ASSETS["tailwind/tailwind.js"])


def test_csp_allows_no_third_party_script_host() -> None:
    assert "cdn.tailwindcss.com" not in CONTENT_SECURITY_POLICY
