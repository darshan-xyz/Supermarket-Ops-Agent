"""Cryptographic password/PIN hashing, constant-time verification, and rate limiting."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import NamedTuple

logger = logging.getLogger("kirana.security")

# Rate limit configuration: max 5 failed attempts per 15 minutes (900 seconds)
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900


class RateLimitStatus(NamedTuple):
    is_locked: bool
    remaining_attempts: int
    lockout_remaining_seconds: int


# In-memory rate limiting state keyed by chat_id or identifier
# Map: chat_id -> list of failed timestamp floats
_FAILED_ATTEMPTS: dict[int, list[float]] = {}


def hash_password(password: str) -> str:
    """Hash a password/PIN using PBKDF2-HMAC-SHA256 with a 16-byte random salt.

    Returns:
        Salted hash string in format: '{salt}${hash_hex}'
    """
    if not password or not password.strip():
        raise ValueError("Password cannot be empty.")
    salt = secrets.token_hex(16)
    hash_hex = hashlib.pbkdf2_hmac(
        "sha256",
        password.strip().encode("utf-8"),
        salt.encode("utf-8"),
        iterations=100_000,
    ).hex()
    return f"{salt}${hash_hex}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Verify a candidate password/PIN against a stored '{salt}${hash}' string.

    Uses constant-time comparison to prevent timing attacks.
    """
    if not password or not stored_hash:
        return False
    try:
        parts = stored_hash.split("$")
        if len(parts) != 2:
            return False
        salt, expected_hash = parts
        computed_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.strip().encode("utf-8"),
            salt.encode("utf-8"),
            iterations=100_000,
        ).hex()
        return secrets.compare_digest(computed_hash, expected_hash)
    except Exception as exc:
        logger.error("Error verifying password: %s", exc)
        return False


def check_pin_rate_limit(chat_id: int) -> RateLimitStatus:
    """Check if the given chat_id is rate-limited / locked out due to repeated failed attempts."""
    now = time.time()
    attempts = _FAILED_ATTEMPTS.get(chat_id, [])
    # Filter attempts within the lockout window
    recent = [t for t in attempts if now - t < LOCKOUT_DURATION_SECONDS]
    _FAILED_ATTEMPTS[chat_id] = recent

    if len(recent) >= MAX_FAILED_ATTEMPTS:
        oldest_in_window = min(recent)
        remaining_lockout = int(LOCKOUT_DURATION_SECONDS - (now - oldest_in_window))
        return RateLimitStatus(
            is_locked=True,
            remaining_attempts=0,
            lockout_remaining_seconds=max(0, remaining_lockout),
        )

    return RateLimitStatus(
        is_locked=False,
        remaining_attempts=MAX_FAILED_ATTEMPTS - len(recent),
        lockout_remaining_seconds=0,
    )


def record_failed_attempt(chat_id: int) -> RateLimitStatus:
    """Record a failed PIN attempt and return current rate limit status."""
    now = time.time()
    attempts = _FAILED_ATTEMPTS.setdefault(chat_id, [])
    attempts.append(now)
    return check_pin_rate_limit(chat_id)


def reset_rate_limit(chat_id: int) -> None:
    """Reset rate limit counter upon successful authentication."""
    _FAILED_ATTEMPTS.pop(chat_id, None)
