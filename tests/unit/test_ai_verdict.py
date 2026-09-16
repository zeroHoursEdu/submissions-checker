from submissions_checker.services.ai_verdict import is_flagged, summarize

CLEAN = {
    "cheating": {"is_cheating": False, "confidence": 0.1, "reason": "original"},
    "ai_generated": {"is_ai_generated": False, "confidence": 0.2, "reason": "human"},
    "code_mark": 82,
    "comment": "Nice",
    "provider": "openai",
    "model": "gpt-test",
}
COPIED = dict(CLEAN, cheating={"is_cheating": True, "confidence": 0.9, "reason": "copied"})


def test_summarize_none_for_missing_verdict() -> None:
    assert summarize(None, {}) is None
    assert summarize({}, {}) is None


def test_clean_verdict_is_not_flagged() -> None:
    s = summarize(CLEAN, {})
    assert s is not None
    assert s.flagged is False
    assert s.code_mark == 82 and s.provider == "openai"


def test_cheating_over_default_threshold_flags() -> None:
    s = summarize(COPIED, {})
    assert s is not None and s.cheating is True and s.flagged is True
    assert is_flagged(COPIED, {}) is True


def test_threshold_from_config_is_respected() -> None:
    assert is_flagged(COPIED, {"cheating_threshold": 0.95}) is False
    aigen = dict(CLEAN, ai_generated={"is_ai_generated": True, "confidence": 0.6, "reason": ""})
    assert is_flagged(aigen, {"ai_generated_threshold": 0.5}) is True
    assert is_flagged(aigen, {"ai_generated_threshold": 0.7}) is False


def test_malformed_fields_are_tolerated() -> None:
    s = summarize({"comment": 3}, None)
    assert s is not None and s.flagged is False and s.code_mark is None and s.comment == "3"
