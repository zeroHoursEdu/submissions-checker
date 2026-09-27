"""Browser-level test: moving to the next question must not count as a violation.

A stepped quiz walks from question to question with a full page navigation. Browsers
announce that navigation with the very events the passive anti-cheat listens for —
Firefox fires `visibilitychange` → hidden (reported as `tab_switch`), Chrome fires
`blur` (reported as `window_blur`) — and the server counts those across the whole
attempt. Before the `leaving` guard, an honest student therefore auto-failed a few
questions in, with `tab_switch` threshold 3 reached purely by answering.

Runs the listeners extracted verbatim from `templates/_quiz_anticheat.html` against a
two-page harness served through Playwright routing: no app server, DB or docker stack.
Firefox carries the regression, so it is parametrised over every installed engine
rather than using the chromium-only `page` fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "_quiz_anticheat.html"

ATTEMPT_ID = 1
ANTI_CHEAT_CONFIG = {
    "rules": [
        {"event": "tab_switch", "threshold": 3, "action": {"type": "fail", "message": "fail"}},
        {"event": "window_blur", "threshold": 4, "action": {"type": "warn", "message": "warn"}},
    ]
}

BASE = "http://quiz.test"
QUESTION_URL = f"{BASE}/portal/quiz/{ATTEMPT_ID}"
ANSWER_URL = f"{BASE}/portal/quiz/{ATTEMPT_ID}/answer"
EVENT_URL = f"{BASE}/portal/quiz/{ATTEMPT_ID}/event"

ENGINES = ["chromium", "firefox", "webkit"]


def _anticheat_js() -> str:
    """The navigation guard plus the passive anti-cheat IIFE straight out of the
    template, with the two Jinja placeholders filled in, so this test always runs the
    current listener code."""
    text = TEMPLATE_PATH.read_text()
    start = text.index("// ─── Intentional-navigation guard")
    end = text.index("})();", text.index("// ─── Anti-cheat event tracking")) + len("})();")
    js = (
        text[start:end]
        .replace("{% if anti_cheat_config %}", "")
        .replace("{{ anti_cheat_config | tojson }}", json.dumps(ANTI_CHEAT_CONFIG))
        .replace("{{ attempt.id }}", str(ATTEMPT_ID))
    )
    assert "{%" not in js and "{{" not in js, "unsubstituted Jinja left in the harness JS"
    return js


def _question_html(number: int) -> str:
    return f"""<!doctype html>
    <html><body>
      <h1>Question {number}</h1>
      <div id="violation-banner" class="hidden"></div>
      <form id="quiz-form" method="POST" action="{ANSWER_URL}">
        <input type="hidden" name="answer" value="a">
        <button type="submit" id="go">Answer</button>
      </form>
      <script>{_anticheat_js()}</script>
    </body></html>"""


@pytest.fixture(params=ENGINES)
def browser(request: pytest.FixtureRequest):
    with sync_playwright() as p:
        try:
            b: Browser = getattr(p, request.param).launch()
        except Exception as exc:  # engine not installed locally
            pytest.skip(f"{request.param} unavailable: {str(exc).splitlines()[0]}")
        yield b
        b.close()


def test_answering_questions_reports_no_violation(browser: Browser) -> None:
    reported: list[str] = []
    page = browser.new_page()
    served = {"n": 1}

    def on_event(route):
        reported.append(json.loads(route.request.post_data or "{}").get("type", "?"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"action": "none", "violation_count": len(reported)}),
        )

    def on_question(route):
        route.fulfill(status=200, content_type="text/html", body=_question_html(served["n"]))

    def on_answer(route):
        served["n"] += 1
        on_question(route)

    page.route(EVENT_URL, on_event)
    page.route(ANSWER_URL, on_answer)
    page.route(QUESTION_URL, on_question)

    page.goto(QUESTION_URL)
    for _ in range(4):
        page.click("#go")
        page.wait_for_load_state("load")
        page.wait_for_timeout(300)
    page.wait_for_timeout(500)

    assert reported == [], f"navigating between questions was reported as: {reported}"


def test_real_violation_is_still_reported(browser: Browser) -> None:
    """The navigation guard must not silence an actual violation on a live page."""
    reported: list[str] = []
    page = browser.new_page()

    def on_event(route):
        reported.append(json.loads(route.request.post_data or "{}").get("type", "?"))
        route.fulfill(status=200, content_type="application/json", body='{"action": "none"}')

    page.route(EVENT_URL, on_event)
    page.route(
        QUESTION_URL,
        lambda route: route.fulfill(status=200, content_type="text/html", body=_question_html(1)),
    )

    page.goto(QUESTION_URL)
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.wait_for_timeout(500)

    assert reported == ["window_blur"]
