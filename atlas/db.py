from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class Database:
    """SQLite-backed durable run/event store.

    Every mutation opens its own connection. BEGIN IMMEDIATE serializes writers,
    which keeps event sequence numbers and state projections consistent.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._schema_lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
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
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS interactions (
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
                );

                CREATE TABLE IF NOT EXISTS tool_executions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    tool_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    input_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS maintenance_events (
                    id TEXT PRIMARY KEY,
                    vehicle_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    odometer_km REAL,
                    notes TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_interactions_run ON interactions(run_id, state);
                CREATE INDEX IF NOT EXISTS idx_maintenance_vehicle ON maintenance_events(vehicle_id, occurred_at DESC);
                """
            )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "target_version": row["target_version"],
            "state": row["state"],
            "input": loads(row["input_json"], {}),
            "context": loads(row["context_json"], {}),
            "output": loads(row["output_json"], None),
            "event_sequence": row["event_sequence"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_run(
        self,
        *,
        target_type: str,
        target_id: str,
        target_version: int,
        input_data: dict[str, Any],
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO runs
                (id, target_type, target_id, target_version, state, input_json,
                 context_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'CREATED', ?, '{}', ?, ?)""",
                (run_id, target_type, target_id, target_version, dumps(input_data), now, now),
            )
            self._append_event_tx(conn, run_id, "run.created", {"target_id": target_id})
        return run_id

    def _append_event_tx(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        row = conn.execute(
            "SELECT event_sequence FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        sequence = int(row[0]) + 1
        now = utc_now()
        conn.execute(
            """INSERT INTO run_events
            (run_id, sequence, type, payload_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)""",
            (run_id, sequence, event_type, dumps(payload), now),
        )
        conn.execute(
            "UPDATE runs SET event_sequence = ?, updated_at = ? WHERE id = ?",
            (sequence, now, run_id),
        )
        return sequence

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self.transaction() as conn:
            return self._append_event_tx(conn, run_id, event_type, payload)

    def transition(
        self,
        run_id: str,
        state: str,
        *,
        context: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            now = utc_now()
            fields = ["state = ?", "updated_at = ?"]
            values: list[Any] = [state, now]
            if context is not None:
                fields.append("context_json = ?")
                values.append(dumps(context))
            if output is not None:
                fields.append("output_json = ?")
                values.append(dumps(output))
            values.append(run_id)
            conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", values)
            self._append_event_tx(
                conn,
                run_id,
                event_type or f"run.{state.lower()}",
                payload or {},
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            return self._row_to_run(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_run(row) for row in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "type": row["type"],
                    "payload": loads(row["payload_json"], {}),
                    "occurred_at": row["occurred_at"],
                }
                for row in rows
            ]

    def create_interaction(
        self,
        *,
        run_id: str,
        kind: str,
        prompt: str,
        payload: dict[str, Any] | None = None,
        artifact_hash: str | None = None,
    ) -> str:
        interaction_id = f"int_{uuid.uuid4().hex}"
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO interactions
                (id, run_id, kind, prompt, payload_json, state, artifact_hash, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                (interaction_id, run_id, kind, prompt, dumps(payload or {}), artifact_hash, now),
            )
            self._append_event_tx(
                conn,
                run_id,
                f"{kind}.requested",
                {"interaction_id": interaction_id, "prompt": prompt},
            )
        return interaction_id

    def pending_interaction(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM interactions
                WHERE run_id = ? AND state = 'PENDING'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "prompt": row["prompt"],
                "payload": loads(row["payload_json"], {}),
                "state": row["state"],
                "artifact_hash": row["artifact_hash"],
            }

    def resolve_interaction(self, interaction_id: str, answer: Any) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown interaction: {interaction_id}")
            if row["state"] != "PENDING":
                raise ValueError("Interaction is already resolved")
            now = utc_now()
            conn.execute(
                """UPDATE interactions
                SET state = 'RESOLVED', answer_json = ?, resolved_at = ?
                WHERE id = ?""",
                (dumps(answer), now, interaction_id),
            )
            self._append_event_tx(
                conn,
                row["run_id"],
                f"{row['kind']}.resolved",
                {"interaction_id": interaction_id, "answer": answer},
            )
            return {
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": loads(row["payload_json"], {}),
                "artifact_hash": row["artifact_hash"],
                "answer": answer,
            }

    def read_maintenance(self, vehicle_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM maintenance_events
                WHERE vehicle_id = ? ORDER BY occurred_at DESC""",
                (vehicle_id,),
            ).fetchall()
            return [dict(row) for row in rows]
