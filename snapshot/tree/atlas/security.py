"""HTTP security primitives (pure, framework-free, offline-testable).

Wired into atlas/app.py. Scope is deliberately the honest minimum for a
single-owner deployment behind HTTPS: a shared bearer token and a per-client
rate limit. No cookies or sessions exist, so CSRF does not apply; user
accounts/OAuth are out of scope until there is more than one user.
"""
from __future__ import annotations

import hmac
import threading
import time
from typing import Callable


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an 'Authorization: Bearer <token>' header."""
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time comparison; False when either side is empty."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class RateLimiter:
    """Fixed-window in-memory rate limiter.

    Suitable for the current single-process deployment; a multi-process
    deployment would move this state into the database or a shared store.
    """

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("limit must be >= 1 and window_s > 0")
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window_s:
                start, count = now, 0
            if count >= self.limit:
                self._windows[key] = (start, count)
                return False
            self._windows[key] = (start, count + 1)
            return True

    def retry_after(self, key: str) -> float:
        with self._lock:
            start, _ = self._windows.get(key, (self.clock(), 0))
        return max(0.0, self.window_s - (self.clock() - start))
