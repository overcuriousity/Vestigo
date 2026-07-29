"""In-memory exponential backoff for failed login attempts.

Single-process by design (like ``core.jobs.JobStore``): the deployment model
is one Uvicorn process, so a shared in-memory counter is sufficient and keeps
the auth path free of new persistence. State is keyed per
``(username, client IP)`` so an attacker hammering one account from one
address is throttled without locking the legitimate user out from elsewhere.

Argon2 slows a single verification; this slows the loop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from vestigo.core.config import get_settings


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0


class LoginBackoff:
    """Tracks failed logins and computes an exponential retry delay.

    After ``threshold`` consecutive failures for a key, the next attempt is
    blocked for ``base_seconds * 2**(failures - threshold)`` seconds, capped
    at ``max_seconds``. A successful login resets the key.
    """

    def __init__(
        self,
        threshold: int,
        base_seconds: float,
        max_seconds: float,
        max_entries: int = 10_000,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._base = base_seconds
        self._max = max_seconds
        # A cap below 1 cannot be honoured — the entry being registered has to
        # live somewhere — so treat it as 1 rather than silently unbounding.
        self._max_entries = max(1, max_entries)
        self._now = now
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, ip: str | None) -> tuple[str, str]:
        return (username.lower(), ip or "")

    def retry_after(self, username: str, ip: str | None) -> float:
        """Seconds until the next attempt is allowed; 0.0 if allowed now."""
        with self._lock:
            entry = self._entries.get(self._key(username, ip))
            if entry is None:
                return 0.0
            return max(0.0, entry.locked_until - self._now())

    def register_failure(self, username: str, ip: str | None) -> None:
        """Record a failed attempt and arm the next delay if over threshold."""
        with self._lock:
            key = self._key(username, ip)
            if key not in self._entries and len(self._entries) >= self._max_entries:
                self._prune_expired_locked()
                if len(self._entries) >= self._max_entries:
                    self._evict_earliest_locked()
            entry = self._entries.setdefault(key, _Entry())
            entry.failures += 1
            if entry.failures >= self._threshold:
                delay = min(self._base * 2 ** (entry.failures - self._threshold), self._max)
                entry.locked_until = self._now() + delay

    def reset(self, username: str, ip: str | None) -> None:
        """Clear state for a key after a successful login."""
        with self._lock:
            self._entries.pop(self._key(username, ip), None)

    def _prune_expired_locked(self) -> None:
        """Drop entries whose lock has expired (caller holds the lock).

        Expired entries lose their failure count — acceptable: an attacker
        only benefits after having already waited out a full delay window.
        """
        now = self._now()
        expired = [k for k, e in self._entries.items() if e.locked_until <= now]
        for key in expired:
            del self._entries[key]

    def _evict_earliest_locked(self) -> None:
        """Drop the entry whose lock expires soonest (caller holds the lock).

        Last resort when pruning frees nothing — every tracked key locked into
        the future — so that ``max_entries`` is an actual bound rather than a
        hint. There is no way to free a slot and keep the evicted key's state:
        the entry *is* the slot. So eviction drops its ``failures`` count too,
        and that key gets ``threshold`` unthrottled attempts before a lock
        re-arms — a larger concession than ``_prune_expired_locked`` makes,
        since prune only discards keys whose delay was already waited out.

        Priced accordingly: to reach this path an attacker must first push
        ``max_entries`` distinct keys past ``threshold`` (~50k requests at the
        defaults), and each further request buys ``threshold`` attempts on
        whichever key is closest to being legitimately released anyway — never
        directly on a chosen victim, whose lock grows exponentially and so
        sorts away from the minimum. Bounded memory is worth that trade.

        O(n) per call, as is ``_prune_expired_locked`` immediately before it.
        Negligible at the default 10k cap; revisit (heap, or insertion-ordered
        eviction) before raising ``max_entries`` by orders of magnitude, since
        both scans run under ``self._lock`` and serialize every login.
        """
        if not self._entries:
            return
        del self._entries[min(self._entries, key=lambda k: self._entries[k].locked_until)]


_default_backoff: LoginBackoff | None = None


def get_login_backoff() -> LoginBackoff:
    """Return the process-wide login backoff tracker."""
    global _default_backoff
    if _default_backoff is None:
        settings = get_settings()
        _default_backoff = LoginBackoff(
            threshold=settings.login_backoff_threshold,
            base_seconds=settings.login_backoff_base_seconds,
            max_seconds=settings.login_backoff_max_seconds,
        )
    return _default_backoff


def reset_login_backoff() -> None:
    """Discard the singleton (test isolation)."""
    global _default_backoff
    _default_backoff = None
