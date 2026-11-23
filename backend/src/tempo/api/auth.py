"""Bearer-token checks for the private API: rotation-friendly comparison and a limiter of
failed attempts per client address (security review items, docs/HANDOFF.md)."""

import hmac
import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

MAX_FAILURES = 10
FAILURE_WINDOW = timedelta(minutes=1)
LOCKOUT = timedelta(minutes=5)
MAX_TRACKED_CLIENTS = 10_000


def token_matches(presented: str | None, accepted: Iterable[bytes]) -> bool:
    """Constant-time comparison against every accepted token (no short-circuit)."""
    candidate = (presented or "").encode()
    result = False
    for token in accepted:
        result |= hmac.compare_digest(candidate, token)
    return result and bool(presented)


class FailureLimiter:
    """After ``max_failures`` failed attempts within ``window``, a client is locked out for
    ``lockout`` (HTTP 429), even with a valid token. In memory: one API replica."""

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window: timedelta = FAILURE_WINDOW,
        lockout: timedelta = LOCKOUT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._max, self._window, self._lockout, self._clock = max_failures, window, lockout, clock
        self._failures: dict[str, deque[datetime]] = {}
        self._locked_until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int | None:
        """Seconds until the client may try again, or None when it is not locked out."""
        with self._lock:
            until = self._locked_until.get(client)
            now = self._clock()
            if until is None:
                return None
            if now >= until:
                del self._locked_until[client]
                return None
            return max(1, int((until - now).total_seconds()))

    def failure(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) >= MAX_TRACKED_CLIENTS and client not in self._failures:
                self._prune(now)
            attempts = self._failures.setdefault(client, deque())
            attempts.append(now)
            while attempts and now - attempts[0] > self._window:
                attempts.popleft()
            if len(attempts) >= self._max:
                self._locked_until[client] = now + self._lockout
                attempts.clear()

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, now: datetime) -> None:
        stale = [k for k, v in self._failures.items() if not v or now - v[-1] > self._window]
        for key in stale:
            del self._failures[key]
        if len(self._failures) >= MAX_TRACKED_CLIENTS:
            self._failures.clear()  # flood of distinct addresses: keep memory bounded
