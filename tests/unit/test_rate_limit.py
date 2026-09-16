from submissions_checker.core.rate_limit import SlidingWindowLimiter


def test_blocks_after_max_failures_within_window() -> None:
    now = [1000.0]
    lim = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    for _ in range(3):
        assert lim.is_blocked("k") is False
        lim.record_failure("k")
    assert lim.is_blocked("k") is True
    now[0] += 61
    assert lim.is_blocked("k") is False


def test_reset_clears_failures() -> None:
    lim = SlidingWindowLimiter(1, 60)
    lim.record_failure("k")
    assert lim.is_blocked("k")
    lim.reset("k")
    assert not lim.is_blocked("k")


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(1, 60)
    lim.record_failure("a")
    assert lim.is_blocked("a") and not lim.is_blocked("b")
