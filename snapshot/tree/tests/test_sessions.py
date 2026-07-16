"""Owner session store (M1-D kernel side)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from atlas.db import Database
from atlas.sessions import SessionStore, _hash


def make(tmp_path: Path, ttl_s: int = 3600) -> tuple[Database, SessionStore]:
    db = Database(str(tmp_path / "atlas.db"))
    return db, SessionStore(db, ttl_s=ttl_s)


def test_create_validate_revoke_roundtrip(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    token = store.create()
    assert store.validate(token) is True
    assert store.validate("forged-token") is False
    assert store.validate(None) is False
    assert store.revoke(token) is True
    assert store.validate(token) is False
    assert store.revoke(token) is False  # already gone


def test_only_hashes_are_stored(tmp_path: Path) -> None:
    db, store = make(tmp_path)
    token = store.create()
    with db.read_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert row["token_hash"] == _hash(token)
    blob = b"".join(
        p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("atlas.db")
    )
    assert token.encode() not in blob  # plaintext never touches disk


def test_expiry_denies_and_removes(tmp_path: Path) -> None:
    db, store = make(tmp_path, ttl_s=3600)
    token = store.create()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", (past,))
    assert store.validate(token) is False
    with db.read_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_purge_expired_only_removes_expired(tmp_path: Path) -> None:
    db, store = make(tmp_path)
    stale = store.create()
    fresh = store.create()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (past, _hash(stale)),
        )
    assert store.purge_expired() == 1
    assert store.validate(fresh) is True
    assert store.validate(stale) is False


def test_last_seen_writes_are_throttled_and_expiry_is_fixed(tmp_path: Path) -> None:
    from atlas import sessions as sessions_module

    db, store = make(tmp_path)
    token = store.create()
    with db.read_conn() as conn:
        before = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()
    assert store.validate(token) is True  # within the throttle window
    with db.read_conn() as conn:
        after = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()
    assert after["last_seen_at"] == before["last_seen_at"]  # no write

    stale = (datetime.now(timezone.utc) - timedelta(
        seconds=sessions_module.LAST_SEEN_MIN_INTERVAL_S + 5
    )).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ?", (stale,))
    assert store.validate(token) is True  # past the window: write happens
    with db.read_conn() as conn:
        final = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()
    assert final["last_seen_at"] != stale
    assert final["expires_at"] == before["expires_at"]  # fixed expiry, no sliding
