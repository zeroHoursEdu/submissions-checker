"""Browser-level test for the passive anti-cheat `keyboard_shortcut` detector.

Exercises the real `keydown` listener extracted verbatim from
`templates/_quiz_anticheat.html` (not a reimplemented copy), so a regression in the
in-template JS predicate fails here instead of only being caught by a person
manually testing every OS/browser combo. Self-contained: no app server, DB, or
docker stack required — only a Chromium instance via pytest-playwright.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "_quiz_anticheat.html"


def _extract_keyboard_shortcut_handler() -> str:
    """Pull the `if (hasRule('keyboard_shortcut')) { ... }` block out of the quiz
    template by brace-counting from its opening brace, so the test always runs
    against whatever the template currently contains."""
    text = TEMPLATE_PATH.read_text()
    start = text.index("if (hasRule('keyboard_shortcut'))")
    open_brace = text.index("{", start)
    depth = 0
    i = open_brace
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return text[start : i + 1]


def _build_harness_html() -> str:
    handler = _extract_keyboard_shortcut_handler()
    return f"""
    <!doctype html>
    <html><body>
    <script>
      window.__reported = [];
      function hasRule(name) {{ return true; }}
      function report(type) {{ window.__reported.push(type); }}
      {handler}
    </script>
    </body></html>
    """


def _dispatch_keydown(page: Page, **init: object) -> dict:
    return page.evaluate(
        """(init) => {
            window.__reported = [];
            const evt = new KeyboardEvent('keydown', {...init, cancelable: true});
            const notPrevented = document.dispatchEvent(evt);
            return {reported: window.__reported.slice(), defaultPrevented: !notPrevented};
        }""",
        init,
    )


def test_windows_ctrl_shortcuts_detected(page: Page) -> None:
    page.set_content(_build_harness_html())
    for key in ["c", "a", "s", "p"]:
        result = _dispatch_keydown(page, key=key, ctrlKey=True)
        assert result["defaultPrevented"], f"Ctrl+{key} should be prevented"
        assert result["reported"] == ["keyboard_shortcut"]


def test_mac_cmd_shortcuts_detected(page: Page) -> None:
    """Regression test for the metaKey gap: Cmd+C/A/S/P on macOS must be
    detected identically to Ctrl+C/A/S/P on Windows/Linux."""
    page.set_content(_build_harness_html())
    for key in ["c", "a", "s", "p"]:
        result = _dispatch_keydown(page, key=key, metaKey=True)
        assert result["defaultPrevented"], f"Cmd+{key} should be prevented"
        assert result["reported"] == ["keyboard_shortcut"]


def test_view_source_shortcut_detected_both_platforms(page: Page) -> None:
    page.set_content(_build_harness_html())
    for modifier in ("ctrlKey", "metaKey"):
        result = _dispatch_keydown(page, key="u", **{modifier: True})
        assert result["defaultPrevented"]
        assert result["reported"] == ["keyboard_shortcut"]


def test_devtools_shortcut_detected_both_platforms(page: Page) -> None:
    page.set_content(_build_harness_html())
    for key in ["i", "j", "c"]:
        windows_result = _dispatch_keydown(page, key=key, ctrlKey=True, shiftKey=True)
        assert windows_result["defaultPrevented"], f"Ctrl+Shift+{key} should be prevented"
        assert windows_result["reported"] == ["keyboard_shortcut"]

        mac_result = _dispatch_keydown(page, key=key, metaKey=True, altKey=True)
        assert mac_result["defaultPrevented"], f"Cmd+Option+{key} should be prevented"
        assert mac_result["reported"] == ["keyboard_shortcut"]


def test_f12_and_printscreen_still_detected(page: Page) -> None:
    page.set_content(_build_harness_html())
    for key in ["F12", "PrintScreen"]:
        result = _dispatch_keydown(page, key=key)
        assert result["defaultPrevented"]
        assert result["reported"] == ["keyboard_shortcut"]


def test_harmless_keys_not_flagged(page: Page) -> None:
    page.set_content(_build_harness_html())
    result = _dispatch_keydown(page, key="x", ctrlKey=True)
    assert not result["defaultPrevented"]
    assert result["reported"] == []

    result = _dispatch_keydown(page, key="a")
    assert not result["defaultPrevented"]
    assert result["reported"] == []
