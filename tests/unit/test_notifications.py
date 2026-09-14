"""Unit tests for notification templates and the dispatcher (pure parts).

Templates are pure (subject, body) builders. The dispatcher's channel-selection
and fan-out logic is exercised with fake settings / fake channels — no SMTP,
no HTTP, no real provider clients are instantiated for the fan-out tests.
"""

from __future__ import annotations

import pytest

from submissions_checker.services.notifications import templates
from submissions_checker.services.notifications.dispatcher import (
    NotificationDispatcher,
    build_dispatcher,
)

# ── templates ─────────────────────────────────────────────────────────────────


def test_submission_reviewed_approve_omits_feedback() -> None:
    subject, body = templates.submission_reviewed_template(
        "Ada", "Lab 1", "approve", "looks great", "http://portal"
    )
    assert "approved" in subject
    assert "Ada" in body
    assert "http://portal" in body
    # feedback only rendered on reject
    assert "Feedback:" not in body


def test_submission_reviewed_reject_includes_feedback() -> None:
    subject, body = templates.submission_reviewed_template(
        "Ada", "Lab 1", "reject", "missing tests", "http://portal"
    )
    assert "rejected" in subject
    assert "Feedback: missing tests" in body


def test_submission_reviewed_reject_without_reason_omits_line() -> None:
    _, body = templates.submission_reviewed_template("Ada", "Lab 1", "reject", "", "http://portal")
    assert "Feedback:" not in body


def test_quiz_result_passed_no_retry_line() -> None:
    subject, body = templates.quiz_result_template(
        "Bo", "Lab 2", 9, 10, is_passed=True, attempts_left=2, portal_url="u"
    )
    assert "passed" in body
    assert "9/10" in body
    assert "attempt(s) remaining" not in body


def test_quiz_result_failed_with_attempts_left() -> None:
    _, body = templates.quiz_result_template(
        "Bo", "Lab 2", 3, 10, is_passed=False, attempts_left=2, portal_url="u"
    )
    assert "did not pass" in body
    assert "2 attempt(s) remaining" in body


def test_quiz_result_failed_no_attempts_left() -> None:
    _, body = templates.quiz_result_template(
        "Bo", "Lab 2", 3, 10, is_passed=False, attempts_left=0, portal_url="u"
    )
    assert "All attempts have been used." in body


def test_quiz_result_failed_attempts_none_uses_used_message() -> None:
    _, body = templates.quiz_result_template(
        "Bo", "Lab 2", 3, 10, is_passed=False, attempts_left=None, portal_url="u"
    )
    assert "All attempts have been used." in body


def test_teacher_digest_singular_vs_plural() -> None:
    subj1, _ = templates.teacher_digest_template("T", [("S", "A", "url")], "dash")
    assert subj1.startswith("1 submission ")
    subj2, body2 = templates.teacher_digest_template(
        "T", [("S1", "A1", "u1"), ("S2", "A2", "u2")], "dash"
    )
    assert subj2.startswith("2 submissions ")
    # each item rendered as a line
    assert "S1" in body2 and "S2" in body2
    assert "u1" in body2 and "u2" in body2
    assert "dash" in body2


def test_deadline_reminder_mentions_subject_and_deadline() -> None:
    subject, body = templates.deadline_reminder_template(
        "Cy", "Lab 3", "Algorithms", "2026-07-01", "http://p"
    )
    assert "Lab 3" in subject
    assert "Algorithms" in body
    assert "2026-07-01" in body


def test_new_submission_template_addresses_teacher() -> None:
    subject, body = templates.new_submission_template("Prof", "Dee", "Lab 4", "http://review")
    assert "Dee" in subject and "Lab 4" in subject
    assert "Prof" in body
    assert "http://review" in body


def test_password_reset_template() -> None:
    subject, body = templates.password_reset_template("Eve", "http://reset")
    assert "password" in subject.lower()
    assert "http://reset" in body


@pytest.mark.parametrize("passed_fn", [templates.passed_template, templates.failed_template])
def test_quiz_pass_fail_github_templates(passed_fn) -> None:
    subject, body = passed_fn("octocat", 8, 10, 5)
    assert "Lab 5" in subject
    assert "@octocat" in body
    assert "8/10" in body


def test_credentials_template_contains_credentials() -> None:
    _, body = templates.credentials_template("Fay", "fay99", "pw123", "http://login")
    assert "fay99" in body
    assert "pw123" in body
    assert "http://login" in body


def test_feedback_request_template() -> None:
    subject, body = templates.feedback_request_template(
        "Gil", "Databases", "Fall 2026", "http://fb"
    )
    assert "Databases" in subject and "Fall 2026" in subject
    assert "http://fb" in body


# ── dispatcher fan-out ────────────────────────────────────────────────────────


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


async def test_dispatcher_fans_out_to_all_channels() -> None:
    c1, c2 = _FakeChannel(), _FakeChannel()
    disp = NotificationDispatcher([c1, c2])
    await disp.notify("a@b.com", "subj", "body")
    assert c1.sent == [("a@b.com", "subj", "body")]
    assert c2.sent == [("a@b.com", "subj", "body")]


async def test_dispatcher_with_no_channels_is_noop() -> None:
    disp = NotificationDispatcher([])
    await disp.notify("a@b.com", "s", "b")  # must not raise


# ── build_dispatcher channel selection ────────────────────────────────────────


class _Cfg:
    """Bare settings stand-in; only attributes set in kwargs exist."""

    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _channel_types(disp: NotificationDispatcher) -> list[str]:
    return [type(c).__name__ for c in disp._channels]


def test_build_dispatcher_no_config_has_no_channels() -> None:
    disp = build_dispatcher(_Cfg())
    assert disp._channels == []


def test_build_dispatcher_adds_resend_when_key_present() -> None:
    disp = build_dispatcher(_Cfg(resend_api_key="re_123", resend_from_address="x@y.com"))
    assert "ResendChannel" in _channel_types(disp)


def test_build_dispatcher_adds_brevo_when_key_present() -> None:
    disp = build_dispatcher(_Cfg(brevo_api_key="b_123", brevo_from_address="x@y.com"))
    assert "BrevoChannel" in _channel_types(disp)


def test_build_dispatcher_adds_smtp_when_host_present() -> None:
    disp = build_dispatcher(
        _Cfg(smtp_host="smtp.example.com", smtp_port=25, smtp_from_address="x@y.com")
    )
    assert "EmailChannel" in _channel_types(disp)


def test_build_dispatcher_all_channels_when_all_configured() -> None:
    disp = build_dispatcher(
        _Cfg(
            resend_api_key="r",
            brevo_api_key="b",
            smtp_host="smtp.example.com",
        )
    )
    types = _channel_types(disp)
    assert {"ResendChannel", "BrevoChannel", "EmailChannel"} <= set(types)


def test_build_dispatcher_empty_strings_are_falsy() -> None:
    # empty api key / host -> channel not added
    disp = build_dispatcher(_Cfg(resend_api_key="", brevo_api_key="", smtp_host=""))
    assert disp._channels == []
