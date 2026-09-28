"""Browser-level test: anti-cheat listeners attach the context the server whitelists.

Runs the real listener blocks extracted from templates/_quiz_anticheat.html, with a
recording `report` stub, in Chromium via the same `browser` fixture pattern as
`test_quiz_anticheat_navigation.py` in this directory — NOT pytest-playwright's own
`page` fixture. Both files launch off the plugin's session-scoped `playwright`
fixture rather than opening a second, independent `sync_playwright()` session:
almost every other file under `tests/e2e/` drives its BDD steps through the
plugin's `page` fixture, and once that fixture's machinery has run anywhere in the
session, a later bare `with sync_playwright() as p:` call — from either this file
or `test_quiz_anticheat_navigation.py` — hits Playwright's "already inside a
running asyncio loop" guard, regardless of which of the two files runs second.
Depending on the plugin's own `playwright` fixture instead reuses that already-open
connection, so both custom multi-engine `browser` fixtures coexist with the rest of
`tests/e2e/` in one `make e2e` run. No app server needed for these tests themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Browser, Playwright

TEMPLATE = (Path(__file__).resolve().parents[2] / "templates" / "_quiz_anticheat.html").read_text()

ENGINES = ["chromium", "firefox", "webkit"]


def _block(marker: str) -> str:
    """The `if (...) { ... }` block starting at `marker`, found by brace counting."""
    start = TEMPLATE.index(marker)
    i = TEMPLATE.index("{", start)
    depth = 0
    while True:
        depth += {"{": 1, "}": -1}.get(TEMPLATE[i], 0)
        if depth == 0:
            return TEMPLATE[start : i + 1]
        i += 1


def _harness_html() -> str:
    blocks = "\n".join(
        _block(m)
        for m in (
            "if (hasRule('tab_switch'))",
            "if (hasRule('window_blur'))",
            "if (hasRule('resize'))",
            "if (hasRule('keyboard_shortcut'))",
        )
    )
    return f"""<!doctype html><html><body><script>
    window.__reported = [];
    window.__hidden = false;
    Object.defineProperty(document, 'hidden', {{configurable: true, get: () => window.__hidden}});
    function hasRule(name) {{ return true; }}
    function report(type, extra) {{ window.__reported.push({{type, extra: extra || null}}); }}
    {blocks}
    </script></body></html>"""


@pytest.fixture(params=ENGINES)
def browser(request: pytest.FixtureRequest, playwright: Playwright):
    try:
        b: Browser = getattr(playwright, request.param).launch()
    except Exception as exc:  # engine not installed locally
        pytest.skip(f"{request.param} unavailable: {str(exc).splitlines()[0]}")
    yield b
    b.close()


def _load(browser: Browser, **new_page_kwargs: object):
    page = browser.new_page(**new_page_kwargs)
    page.set_content(_harness_html())
    return page


def test_tab_return_reports_time_away(browser: Browser) -> None:
    page = _load(browser)
    page.evaluate("window.__hidden = true; document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(120)
    page.evaluate("window.__hidden = false; document.dispatchEvent(new Event('visibilitychange'))")
    reported = page.evaluate("window.__reported")
    assert [r["type"] for r in reported] == ["tab_switch", "tab_return"]
    assert reported[1]["extra"]["away_ms"] >= 100


def test_focus_return_reports_time_away(browser: Browser) -> None:
    page = _load(browser)
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.wait_for_timeout(60)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    reported = page.evaluate("window.__reported")
    assert [r["type"] for r in reported] == ["window_blur", "focus_return"]
    assert reported[1]["extra"]["away_ms"] >= 50


def test_focus_without_prior_blur_reports_nothing(browser: Browser) -> None:
    page = _load(browser)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    assert page.evaluate("window.__reported") == []


def test_keyboard_shortcut_reports_the_combo_only(browser: Browser) -> None:
    page = _load(browser)
    page.evaluate(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', ctrlKey: true, cancelable: true}))"
    )
    page.evaluate(
        "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'F12', cancelable: true}))"
    )
    extras = [r["extra"] for r in page.evaluate("window.__reported")]
    assert extras == [{"combo": "ctrl+c"}, {"combo": "f12"}]


def test_resize_reports_from_and_to(browser: Browser) -> None:
    page = _load(browser, viewport={"width": 1200, "height": 900})
    page.set_viewport_size({"width": 1200, "height": 500})
    page.wait_for_timeout(100)
    (r,) = page.evaluate("window.__reported")
    assert r["type"] == "resize"
    assert r["extra"] == {"from_w": 1200, "from_h": 900, "to_w": 1200, "to_h": 500}
