"""Password hashing and session-token helpers for local authentication."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2 hash of ``password``."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Return True if ``password`` matches ``password_hash``.

    Returns False (rather than raising) for a missing hash — e.g. an
    OIDC-only account that has never set a local password — and for any
    verification failure.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed/legacy hash — treat as a non-match rather than a 500.
        return False


def new_session_token() -> str:
    """Return a new cryptographically-random, URL-safe session identifier."""
    return secrets.token_urlsafe(32)


def session_expiry(ttl_hours: int) -> datetime:
    """Return the UTC expiry timestamp for a session created now with the given TTL."""
    return datetime.now(UTC) + timedelta(hours=ttl_hours)
