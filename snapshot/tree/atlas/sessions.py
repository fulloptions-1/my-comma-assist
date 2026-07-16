"""Owner browser sessions.

Login (atlas/app.py) verifies the single owner access secret in constant
time, then issues a random session token delivered as an HttpOnly cookie.
Only the SHA-256 hash of the token is stored; the plaintext exists in the
cookie alone, so a database copy cannot mint sessions. Bearer-token API
access for programmatic clients is unchanged and lives in atlas/security.py.

Expiry is FIXED: expires_at is set once at creation and never slides.
last_seen_at is telemetry only, and its writes are throttled to once per
LAST_SEEN_MIN_INTERVAL_S so that a polling UI does not hold the SQLite
writer lock on every request (M1.1 §6).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from atlas.db import Database

DEFAULT_TTL_S = 30 * 24 * 3600  # 30 days
LAST_SEEN_MIN_INTERVAL_S = 300  # throttle telemetry writes (M1.1 §6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    def __init__(self, db: Database, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.db = db
        self.ttl_s = ttl_s

    def create(self) -> str:
        """Mint a new session; returns the plaintext token (cookie value)."""
        token = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(seconds=self.ttl_s)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, created_at, expires_at, last_seen_at)"
                " VALUES (?, ?, ?, ?)",
                (_hash(token), now.isoformat(), expires.isoformat(), now.isoformat()),
            )
        return token

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        with self.db.read_conn() as conn:
            row = conn.execute(
                "SELECT expires_at, last_seen_at FROM sessions WHERE token_hash = ?",
                (_hash(token),),
            ).fetchone()
        if row is None:
            return False
        now = _now()
        if datetime.fromisoformat(row["expires_at"]) <= now:
            self.revoke(token)  # expired: remove eagerly
            return False
        last_seen = datetime.fromisoformat(row["last_seen_at"])
        if (now - last_seen).total_seconds() >= LAST_SEEN_MIN_INTERVAL_S:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (now.isoformat(), _hash(token)),
                )
        return True

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_hash(token),)
            )
            return cursor.rowcount > 0

    def purge_expired(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (_now().isoformat(),)
            )
            return cursor.rowcount
