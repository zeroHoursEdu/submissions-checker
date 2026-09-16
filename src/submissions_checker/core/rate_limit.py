"""In-memory sliding-window throttle for credential endpoints.

Process-local on purpose: the production stack runs two replicas, so an attacker
gets at most 2x the configured budget - still a hard stop on online guessing, with
no Redis to run. Successful logins reset their key.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

from submissions_checker.core.config import get_settings


class SlidingWindowLimiter:
    def __init__(
        self,
        max_attempts: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events.setdefault(key, deque())
        cutoff = now - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self._clock())) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


_login_limiter: SlidingWindowLimiter | None = None


def get_login_limiter() -> SlidingWindowLimiter:
    """The process-wide limiter shared by login and forgot-password."""
    global _login_limiter
    if _login_limiter is None:
        s = get_settings()
        _login_limiter = SlidingWindowLimiter(s.login_max_attempts, s.login_window_seconds)
    return _login_limiter


def client_ip(request: Request) -> str:
    """First X-Forwarded-For hop (Caddy sets it in production) or the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
