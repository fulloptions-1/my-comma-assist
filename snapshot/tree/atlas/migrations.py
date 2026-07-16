"""Numbered database migrations.

Each migration is a tuple of individual SQL statements applied inside ONE
transaction together with its `schema_migrations` version row — a migration
either fully applies and is recorded, or fully rolls back with a loud error.
(`executescript` is never used: it auto-commits and would break atomicity.)

Migrations 0001–0003 are deliberately idempotent (IF NOT EXISTS) so that
databases created before this mechanism existed — the original vertical
slice and the S1–S7 kernel — are adopted safely: pending migrations run
harmlessly over the existing objects and the version rows are recorded.
Migrations added after 0003 may be non-idempotent; they rely on the version
records from this point on.

Operational notes (backup/rollback) live in docs/RAILWAY_DEPLOYMENT.md.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


class MigrationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


Migration = tuple[int, str, tuple[str, ...]]

MIGRATIONS: list[Migration] = [
    (
        1,
        "vertical_slice_core",
        (
            """CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_version INTEGER NOT NULL,
                state TEXT NOT NULL,
                input_json TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT,
                event_sequence INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            )""",
            """CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                answer_json TEXT,
                artifact_hash TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS tool_executions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                tool_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                input_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS maintenance_events (
                id TEXT PRIMARY KEY,
                vehicle_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                odometer_km REAL,
                notes TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_run ON interactions(run_id, state)",
            "CREATE INDEX IF NOT EXISTS idx_maintenance_vehicle ON maintenance_events(vehicle_id, occurred_at DESC)",
        ),
    ),
    (
        2,
        "s1_s7_kernel",
        (
            """CREATE TABLE IF NOT EXISTS resource_locks (
                lock_key TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_encrypted BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state)",
        ),
    ),
    (
        3,
        "sessions_and_config",
        (
            # Browser sessions: only SHA-256 hashes of session tokens are stored.
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )""",
            # Non-secret runtime configuration (model profiles, provider
            # endpoints). Secrets stay in `settings` (encrypted).
            """CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
        ),
    ),
    (
        4,
        "0004_tool_execution_owner",
        (
            # Which process instance holds a RUNNING claim (M1.2 §2). NULL for
            # rows written before this migration; startup reclaim treats NULL
            # and foreign owners as belonging to a dead previous process.
            "ALTER TABLE tool_executions ADD COLUMN owner TEXT",
        ),
    ),
]


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )"""
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_ledger(conn)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    done = applied_versions(conn)
    return max(done) if done else 0


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order. Returns versions applied now."""
    done = applied_versions(conn)
    applied_now: list[int] = []
    for version, name, statements in MIGRATIONS:
        if version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _utc_now()),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(
                f"migration {version:04d}_{name} failed and was rolled back: {exc}"
            ) from exc
        applied_now.append(version)
    return applied_now
