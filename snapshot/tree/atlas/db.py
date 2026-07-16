from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def _answer_event_payload(
    interaction_id: str, answer: Any, redact: bool
) -> dict[str, Any]:
    """Event payload for a resolved interaction.

    Redaction policy (M1.2 §8): free-text answers to model questions may
    contain anything the user typed, including secrets, so their VALUE never
    enters the ordinary event stream — only length and a hash prefix for
    correlation. Structured answers (approval booleans, numeric mileage,
    capability choices) are recorded verbatim. The interactions row itself
    always keeps the full answer; it is the functional record, not telemetry.
    """
    if not redact:
        return {"interaction_id": interaction_id, "answer": answer}
    blob = dumps(answer)
    return {
        "interaction_id": interaction_id,
        "answer": "[redacted]",
        "answer_length": len(blob),
        "answer_sha256_12": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12],
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


# ---------------------------------------------------------------------------
# Explicit run state machine. Deterministic code owns state: every transition
# is validated against this table inside the write transaction, and terminal
# states admit no successors.
# ---------------------------------------------------------------------------

TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
WAITING_STATES = frozenset({"WAITING_FOR_USER", "WAITING_FOR_APPROVAL"})

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset(
        {"WAITING_FOR_USER", "WAITING_FOR_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}
    ),
    # WAITING_FOR_APPROVAL is reachable directly: a deterministic next
    # question is created atomically with resolving the previous one
    # (resolve_and_requeue_interaction, M1.2 §4) — no RUNNING hop to crash in.
    "WAITING_FOR_USER": frozenset(
        {"RUNNING", "WAITING_FOR_APPROVAL", "FAILED", "CANCELLED"}
    ),
    "WAITING_FOR_APPROVAL": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


class IllegalTransitionError(ValueError):
    """A run state change violates the legal transition table (or a CAS guard)."""


class Database:
    """SQLite-backed durable run/event store.

    Every mutation opens its own connection. BEGIN IMMEDIATE serializes writers,
    which keeps event sequence numbers and state projections consistent. Read
    paths use short-lived connections that are always closed.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._schema_lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        """Short-lived read connection that is always closed (fd-leak safe)."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

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

    def migrate(self) -> None:
        """Apply pending numbered migrations (see atlas/migrations.py)."""
        from atlas import migrations

        with self._schema_lock, self.read_conn() as conn:
            migrations.apply_migrations(conn)

    def get_config(self, key: str, default: Any = None) -> Any:
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT value_json FROM config WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else loads(row["value_json"], default)

    def set_config(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO config (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
                (key, dumps(value), utc_now()),
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
        context: dict[str, Any] | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO runs
                (id, target_type, target_id, target_version, state, input_json,
                 context_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'CREATED', ?, ?, ?, ?)""",
                (
                    run_id,
                    target_type,
                    target_id,
                    target_version,
                    dumps(input_data),
                    dumps(context or {}),
                    now,
                    now,
                ),
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

    # -- state transitions ---------------------------------------------------

    def _validate_transition_tx(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        new_state: str,
        expected_state: str | Iterable[str] | None,
    ) -> str:
        row = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        current = str(row["state"])
        if new_state not in LEGAL_TRANSITIONS:
            raise IllegalTransitionError(f"Unknown run state: {new_state!r}")
        if expected_state is not None:
            expected = (
                {expected_state} if isinstance(expected_state, str) else set(expected_state)
            )
            if current not in expected:
                raise IllegalTransitionError(
                    f"Run {run_id} is {current}, expected {sorted(expected)}"
                )
        if new_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
            raise IllegalTransitionError(
                f"Illegal transition {current} -> {new_state} for run {run_id}"
            )
        return current

    def _apply_transition_tx(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        state: str,
        *,
        context: dict[str, Any] | None,
        output: dict[str, Any] | None,
        event_type: str | None,
        payload: dict[str, Any] | None,
    ) -> None:
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

    def transition(
        self,
        run_id: str,
        state: str,
        *,
        context: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_state: str | Iterable[str] | None = None,
    ) -> None:
        with self.transaction() as conn:
            self._validate_transition_tx(conn, run_id, state, expected_state)
            self._apply_transition_tx(
                conn,
                run_id,
                state,
                context=context,
                output=output,
                event_type=event_type,
                payload=payload,
            )

    def update_context(
        self,
        run_id: str,
        context: dict[str, Any],
        *,
        event_type: str = "run.context_updated",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist context and append an event without changing run state."""
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if row["state"] in TERMINAL_STATES:
                raise IllegalTransitionError(
                    f"Cannot update context of terminal run {run_id}"
                )
            conn.execute(
                "UPDATE runs SET context_json = ?, updated_at = ? WHERE id = ?",
                (dumps(context), utc_now(), run_id),
            )
            self._append_event_tx(conn, run_id, event_type, payload or {})

    def transition_with_interaction(
        self,
        run_id: str,
        state: str,
        *,
        kind: str,
        prompt: str,
        payload: dict[str, Any] | None = None,
        artifact_hash: str | None = None,
        context: dict[str, Any] | None = None,
        expected_state: str | Iterable[str] | None = None,
    ) -> str:
        """Atomically enter a waiting state AND create its interaction.

        A crash can no longer leave a waiting run with nothing to answer:
        the state change, the interaction row, and both events commit together.
        """
        if state not in WAITING_STATES:
            raise IllegalTransitionError(
                f"transition_with_interaction requires a waiting state, got {state!r}"
            )
        interaction_id = f"int_{uuid.uuid4().hex}"
        now = utc_now()
        with self.transaction() as conn:
            self._validate_transition_tx(conn, run_id, state, expected_state)
            self._apply_transition_tx(
                conn,
                run_id,
                state,
                context=context,
                output=None,
                event_type=f"run.{state.lower()}",
                payload={},
            )
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

    # -- reads ----------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.read_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            return self._row_to_run(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_run(row) for row in rows]

    def runs_in_states(self, states: Iterable[str]) -> list[dict[str, Any]]:
        wanted = sorted(set(states))
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self.read_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE state IN ({placeholders}) ORDER BY created_at",
                wanted,
            ).fetchall()
            return [self._row_to_run(row) for row in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
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

    # -- interactions -----------------------------------------------------------

    @staticmethod
    def _row_to_interaction(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "kind": row["kind"],
            "prompt": row["prompt"],
            "payload": loads(row["payload_json"], {}),
            "state": row["state"],
            "artifact_hash": row["artifact_hash"],
        }

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

    def get_interaction(self, interaction_id: str) -> dict[str, Any]:
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown interaction: {interaction_id}")
            return self._row_to_interaction(row)

    def pending_interaction(self, run_id: str) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute(
                """SELECT * FROM interactions
                WHERE run_id = ? AND state = 'PENDING'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_interaction(row)

    def resolve_and_requeue_interaction(
        self,
        interaction_id: str,
        answer: Any,
        *,
        run_id: str,
        expected_kind: str,
        new_state: str,
        next_kind: str,
        next_prompt: str,
        next_payload: dict[str, Any] | None = None,
        next_artifact_hash: str | None = None,
        context: dict[str, Any] | None = None,
        event_type: str = "run.waiting",
        payload: dict[str, Any] | None = None,
        expected_state: str | Iterable[str] | None = None,
        redact_answer: bool = False,
    ) -> str:
        """Resolve one interaction and create the deterministic NEXT one in a
        single transaction (M1.2 §4): answer, context, state change, new
        question, and all events commit together — there is no intermediate
        RUNNING hop for a crash to orphan. Returns the new interaction id."""
        next_id = f"int_{uuid.uuid4().hex}"
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown interaction: {interaction_id}")
            if row["run_id"] != run_id:
                raise ValueError("Interaction does not belong to this run")
            if row["kind"] != expected_kind:
                raise ValueError(
                    f"Interaction kind is {row['kind']!r}, expected {expected_kind!r}"
                )
            if row["state"] != "PENDING":
                raise ValueError("Interaction is already resolved")
            self._validate_transition_tx(conn, run_id, new_state, expected_state)
            conn.execute(
                """UPDATE interactions
                SET state = 'RESOLVED', answer_json = ?, resolved_at = ?
                WHERE id = ?""",
                (dumps(answer), now, interaction_id),
            )
            self._append_event_tx(
                conn,
                run_id,
                f"{row['kind']}.resolved",
                _answer_event_payload(interaction_id, answer, redact_answer),
            )
            conn.execute(
                """INSERT INTO interactions
                (id, run_id, kind, prompt, payload_json, state, artifact_hash, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                (next_id, run_id, next_kind, next_prompt,
                 dumps(next_payload or {}), next_artifact_hash, now),
            )
            self._append_event_tx(
                conn,
                run_id,
                f"{next_kind}.requested",
                {"interaction_id": next_id, "prompt": next_prompt},
            )
            self._apply_transition_tx(
                conn,
                run_id,
                new_state,
                context=context,
                output=None,
                event_type=event_type,
                payload=payload,
            )
        return next_id

    def resolve_interaction_and_transition(
        self,
        interaction_id: str,
        answer: Any,
        *,
        run_id: str,
        expected_kind: str,
        new_state: str,
        context: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_state: str | Iterable[str] | None = None,
        redact_answer: bool = False,
    ) -> None:
        """Resolve an interaction AND move the run in ONE transaction.

        Crash-safety for approvals (M1.1 §5): there is no window in which the
        decision is recorded but the run (and its committed context, e.g. the
        exact executing_approval action) is not, or vice versa.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown interaction: {interaction_id}")
            if row["run_id"] != run_id:
                raise ValueError("Interaction does not belong to this run")
            if row["kind"] != expected_kind:
                raise ValueError(
                    f"Interaction kind is {row['kind']!r}, expected {expected_kind!r}"
                )
            if row["state"] != "PENDING":
                raise ValueError("Interaction is already resolved")
            self._validate_transition_tx(conn, run_id, new_state, expected_state)
            now = utc_now()
            conn.execute(
                """UPDATE interactions
                SET state = 'RESOLVED', answer_json = ?, resolved_at = ?
                WHERE id = ?""",
                (dumps(answer), now, interaction_id),
            )
            self._append_event_tx(
                conn,
                run_id,
                f"{row['kind']}.resolved",
                _answer_event_payload(interaction_id, answer, redact_answer),
            )
            self._apply_transition_tx(
                conn,
                run_id,
                new_state,
                context=context,
                output=output,
                event_type=event_type,
                payload=payload,
            )

    def resolve_interaction(
        self,
        interaction_id: str,
        answer: Any,
        *,
        expected_run_id: str | None = None,
        expected_kind: str | None = None,
        redact_answer: bool = False,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown interaction: {interaction_id}")
            if expected_run_id is not None and row["run_id"] != expected_run_id:
                raise ValueError("Interaction does not belong to this run")
            if expected_kind is not None and row["kind"] != expected_kind:
                raise ValueError(
                    f"Interaction kind is {row['kind']!r}, expected {expected_kind!r}"
                )
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
                _answer_event_payload(interaction_id, answer, redact_answer),
            )
            return {
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": loads(row["payload_json"], {}),
                "artifact_hash": row["artifact_hash"],
                "answer": answer,
            }

    # -- domain -----------------------------------------------------------------

    def read_maintenance(
        self,
        vehicle_id: str,
        event_type: str | None = None,
        event_types: "Iterable[str] | None" = None,
    ) -> list[dict[str, Any]]:
        """Maintenance history, newest first; optionally filtered to one or
        more event types (M1.4 §4, widened in M1.5 §2 so category questions
        like 'last oil change' can span oil_change AND
        oil_and_filter_change)."""
        wanted = tuple(event_types or ())
        if event_type and not wanted:
            wanted = (event_type,)
        with self.read_conn() as conn:
            if wanted:
                placeholders = ", ".join("?" for _ in wanted)
                rows = conn.execute(
                    f"""SELECT * FROM maintenance_events
                    WHERE vehicle_id = ? AND event_type IN ({placeholders})
                    ORDER BY occurred_at DESC, created_at DESC""",
                    (vehicle_id, *wanted),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM maintenance_events
                    WHERE vehicle_id = ? ORDER BY occurred_at DESC, created_at DESC""",
                    (vehicle_id,),
                ).fetchall()
            return [dict(row) for row in rows]
