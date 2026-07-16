from __future__ import annotations

import hashlib
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from atlas.db import Database, dumps, loads, utc_now
from atlas.providers import ToolDef


def canonical_dumps(value) -> str:
    """Order-insensitive JSON for hashing: identical inputs hash identically
    regardless of key insertion order."""
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Error taxonomy. The gateway — deterministic code — owns permissions, input
# validation, idempotency, locking, timeouts, and audit events.
# ---------------------------------------------------------------------------


class ToolPermissionError(PermissionError):
    """Caller's pinned allow-list does not include the requested tool."""


class ToolInputError(ValueError):
    """Tool input failed schema validation (raised before any row is written)."""


class ToolLockTimeout(RuntimeError):
    """Could not acquire the tool's resource lock within the wait budget."""


class ToolExecutionTimeout(RuntimeError):
    """Tool handler exceeded its per-tool timeout."""


@dataclass(frozen=True)
class FieldSpec:
    type: type
    required: bool = False
    default: object = None


@dataclass(frozen=True)
class ToolSpec:
    id: str
    side_effect: bool
    handler: Callable[[dict[str, Any], str], dict[str, Any]]
    schema: Mapping[str, FieldSpec] = field(default_factory=dict)
    timeout_s: float = 10.0
    lock_key: str | None = None  # e.g. "vehicle:{vehicle_id}"
    description: str = ""


JSON_TYPES = {str: "string", float: "number", int: "integer", bool: "boolean", list: "array"}


EVENT_RESULT_LIMIT = 2_000  # chars of serialized result allowed in an event payload
LOCK_LEASE_S = 30.0
LOCK_WAIT_S = 10.0
EXECUTION_LEASE_S = 300.0  # RUNNING older than this is presumed crashed


class ToolGateway:
    def __init__(self, db: Database) -> None:
        self.db = db
        # One id per gateway/process instance: claims carry their owner so a
        # restart can tell dead-process work from genuinely live work instead
        # of guessing from row age alone (M1.2 §2).
        self.instance_id = f"proc_{uuid.uuid4().hex}"
        self.tools: dict[str, ToolSpec] = {}
        self.register(
            ToolSpec(
                id="car.read_history",
                side_effect=False,
                description=(
                    "Read the logged maintenance history for a vehicle, newest "
                    "first; optionally filtered to a single event_type."
                ),
                handler=self._read_history,
                schema={
                    "vehicle_id": FieldSpec(str, default="genesis-2016"),
                    "event_type": FieldSpec(str, default=""),
                    # One question can span several concrete types, e.g.
                    # "last oil change" covers oil_change AND
                    # oil_and_filter_change (M1.5 §2).
                    "event_types": FieldSpec(list),
                },
                timeout_s=10.0,
            )
        )
        self.register(
            ToolSpec(
                id="car.log_maintenance",
                side_effect=True,
                description="Append a maintenance event to the vehicle log (side effect).",
                handler=self._log_maintenance,
                schema={
                    "vehicle_id": FieldSpec(str, default="genesis-2016"),
                    "event_type": FieldSpec(str, required=True),
                    "odometer_km": FieldSpec(float, required=True),
                    "notes": FieldSpec(str),
                    "occurred_at": FieldSpec(str),
                },
                timeout_s=10.0,
                lock_key="vehicle:{vehicle_id}",
            )
        )

    def register(self, spec: ToolSpec) -> None:
        if spec.id in self.tools:
            raise ValueError(f"Duplicate tool id: {spec.id}")
        self.tools[spec.id] = spec

    @property
    def known_tools(self) -> set[str]:
        return set(self.tools)

    def tool_defs(self, allowed: Iterable[str]) -> tuple[ToolDef, ...]:
        """Provider-facing tool definitions for the caller's allow-list."""
        defs: list[ToolDef] = []
        for tool_id in allowed:
            spec = self.tools.get(tool_id)
            if spec is None:
                continue
            defs.append(
                ToolDef(
                    name=spec.id,
                    description=spec.description or spec.id,
                    schema={
                        name: {
                            "type": JSON_TYPES.get(fspec.type, "string"),
                            "required": fspec.required,
                        }
                        for name, fspec in spec.schema.items()
                    },
                )
            )
        return tuple(defs)

    # ------------------------------------------------------------------ execute

    def execute(
        self,
        *,
        run_id: str,
        tool_id: str,
        parameters: dict[str, Any],
        idempotency_key: str,
        allowed_tools: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if tool_id not in self.tools:
            raise KeyError(f"Unknown tool: {tool_id}")
        spec = self.tools[tool_id]

        # 1. Permission boundary lives here, not in handlers. `None` preserves
        #    the pre-existing internal-caller behavior; every runtime call site
        #    passes the run's pinned blueprint allow-list.
        if allowed_tools is not None and tool_id not in set(allowed_tools):
            self.db.append_event(
                run_id, "tool.denied", {"tool_id": tool_id, "reason": "not in allow-list"}
            )
            raise ToolPermissionError(
                f"Tool {tool_id!r} is not in the caller's allow-list"
            )

        # 2. Validate input BEFORE any durable row exists, so a rejected call
        #    neither burns the idempotency key nor pollutes the audit trail
        #    with half-executions.
        parameters = self._validate_input(spec, parameters)
        input_hash = hashlib.sha256(canonical_dumps(parameters).encode("utf-8")).hexdigest()

        # 3. Claim or reuse the idempotent execution record.
        replay = self._claim_execution(run_id, spec, idempotency_key, input_hash)
        if replay is not None:
            return replay

        # 4. Serialize conflicting writes with a leased resource lock.
        lock_key = spec.lock_key.format(**parameters) if spec.lock_key else None
        holder = f"{run_id}:{idempotency_key}"
        try:
            if lock_key:
                self._acquire_lock(lock_key, holder)
            result, overrun_s = self._run_handler(spec, parameters, idempotency_key)
        except Exception as exc:
            self._mark_failed(run_id, spec, idempotency_key, exc)
            raise
        finally:
            if lock_key:
                self._release_lock(lock_key, holder)

        # 5. Persist completion; trim oversized results out of the event stream.
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE tool_executions
                SET state = 'COMPLETED', result_json = ?, completed_at = ?
                WHERE idempotency_key = ?""",
                (dumps(result), utc_now(), idempotency_key),
            )
            completion: dict[str, Any] = {
                "tool_id": tool_id,
                "result": self._event_result(result),
            }
            if overrun_s is not None:
                # Side effect finished but blew its deadline: recorded, not
                # faked as a cancellation (see _run_handler contract).
                completion["timeout_exceeded"] = True
                completion["elapsed_s"] = round(overrun_s, 3)
            self.db._append_event_tx(conn, run_id, "tool.completed", completion)
        return result

    # -------------------------------------------------------------- validation

    @staticmethod
    def _validate_input(spec: ToolSpec, parameters: dict[str, Any]) -> dict[str, Any]:
        problems: list[str] = []
        cleaned: dict[str, Any] = {}
        unknown = sorted(set(parameters) - set(spec.schema))
        if unknown:
            problems.append(f"unknown fields: {', '.join(unknown)}")
        for name, fspec in spec.schema.items():
            if name not in parameters or parameters[name] is None:
                if fspec.required:
                    problems.append(f"missing required field: {name}")
                elif fspec.default is not None:
                    cleaned[name] = fspec.default
                continue
            value = parameters[name]
            if fspec.type is float and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value):
                    # NaN/inf must never reach SQLite or an approval hash
                    # (M1.4 §8).
                    problems.append(f"{name} must be a finite number")
                    continue
                cleaned[name] = float(value)
            elif isinstance(value, fspec.type) and not (
                fspec.type is not bool and isinstance(value, bool)
            ):
                cleaned[name] = value
            else:
                problems.append(
                    f"field {name!r} expected {fspec.type.__name__}, got {type(value).__name__}"
                )
        if problems:
            raise ToolInputError(f"{spec.id}: " + "; ".join(problems))
        return cleaned

    # -------------------------------------------------- idempotent claim/replay

    def _claim_execution(
        self, run_id: str, spec: ToolSpec, idempotency_key: str, input_hash: str
    ) -> dict[str, Any] | None:
        """Create the RUNNING execution row, or return the replayable result.

        Returns the previous result when the identical call already COMPLETED.
        Raises when the key is reused with different input or is genuinely
        in flight. FAILED and ABANDONED rows with identical input may retry.
        """
        now = utc_now()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO tool_executions
                    (id, run_id, tool_id, idempotency_key, input_hash, state, created_at, owner)
                    VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)""",
                    (f"tool_{uuid.uuid4().hex}", run_id, spec.id, idempotency_key,
                     input_hash, now, self.instance_id),
                )
                self.db._append_event_tx(
                    conn, run_id, "tool.started", {"tool_id": spec.id}
                )
                return None

            if existing["run_id"] != run_id or existing["tool_id"] != spec.id:
                raise ValueError(
                    "Idempotency key is already bound to a different "
                    f"run/tool (run {existing['run_id']!r}, "
                    f"tool {existing['tool_id']!r})"
                )
            if existing["input_hash"] != input_hash:
                raise ValueError("Idempotency key was reused with different input")
            state = existing["state"]
            if state == "COMPLETED":
                self.db._append_event_tx(
                    conn,
                    run_id,
                    "tool.replayed",
                    {"tool_id": spec.id, "idempotency_key": idempotency_key},
                )
                return loads(existing["result_json"], {})
            if state == "RUNNING":
                if existing["created_at"] > self._lease_horizon():
                    # Fresh claims are never stolen here — not ours, not a
                    # foreign worker's. Dead-process rows are reclassified by
                    # reclaim_orphaned_executions() at startup, not by age
                    # guesses at claim time (M1.2 §2).
                    raise RuntimeError("A matching tool execution is already in progress")
                # Presumed crashed: abandon in place and fall through to retry.
                self.db._append_event_tx(
                    conn,
                    existing["run_id"],
                    "tool.abandoned",
                    {"tool_id": spec.id, "idempotency_key": idempotency_key},
                )
            # FAILED / ABANDONED / stale-RUNNING with identical input: retry.
            conn.execute(
                """UPDATE tool_executions
                SET state = 'RUNNING', created_at = ?, completed_at = NULL,
                    result_json = NULL, owner = ?
                WHERE idempotency_key = ?""",
                (now, self.instance_id, idempotency_key),
            )
            self.db._append_event_tx(
                conn,
                run_id,
                "tool.retried",
                {"tool_id": spec.id, "previous_state": state},
            )
            return None

    @staticmethod
    def _lease_horizon() -> str:
        return (
            datetime.now(timezone.utc) - timedelta(seconds=EXECUTION_LEASE_S)
        ).isoformat()

    def reclaim_orphaned_executions(self) -> int:
        """Abandon RUNNING claims owned by a DEAD previous process (M1.2 §2).

        Called once at startup, BEFORE any run recovery, under the current
        single-process deployment model: at boot, every RUNNING claim whose
        owner is not this instance (or is NULL, pre-0004) belonged to the
        process that just died and may be retried safely under the same
        idempotency key. A future multi-worker deployment must replace this
        boot-time sweep with heartbeat leases; claim-time behavior already
        refuses to steal fresh foreign claims, so only this method needs to
        change."""
        reclaimed = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT run_id, tool_id, idempotency_key FROM tool_executions "
                "WHERE state = 'RUNNING' AND (owner IS NULL OR owner != ?)",
                (self.instance_id,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE tool_executions SET state = 'ABANDONED' WHERE idempotency_key = ?",
                    (row["idempotency_key"],),
                )
                self.db._append_event_tx(
                    conn,
                    row["run_id"],
                    "tool.abandoned",
                    {
                        "tool_id": row["tool_id"],
                        "idempotency_key": row["idempotency_key"],
                        "reason": "process_restart",
                    },
                )
                reclaimed += 1
        return reclaimed

    def recover_stale_executions(self) -> int:
        """Mark presumed-crashed RUNNING executions ABANDONED (startup sweep)."""
        horizon = self._lease_horizon()
        recovered = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT run_id, tool_id, idempotency_key FROM tool_executions "
                "WHERE state = 'RUNNING' AND created_at <= ?",
                (horizon,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE tool_executions SET state = 'ABANDONED' WHERE idempotency_key = ?",
                    (row["idempotency_key"],),
                )
                self.db._append_event_tx(
                    conn,
                    row["run_id"],
                    "tool.abandoned",
                    {"tool_id": row["tool_id"], "idempotency_key": row["idempotency_key"]},
                )
                recovered += 1
        return recovered

    # ------------------------------------------------------------------- locks

    def _acquire_lock(self, lock_key: str, holder: str) -> None:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            now = datetime.now(timezone.utc)
            expires = (now + timedelta(seconds=LOCK_LEASE_S)).isoformat()
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT holder, expires_at FROM resource_locks WHERE lock_key = ?",
                    (lock_key,),
                ).fetchone()
                if row is None or row["expires_at"] <= now.isoformat() or row["holder"] == holder:
                    conn.execute(
                        """INSERT INTO resource_locks (lock_key, holder, acquired_at, expires_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(lock_key) DO UPDATE
                        SET holder = excluded.holder,
                            acquired_at = excluded.acquired_at,
                            expires_at = excluded.expires_at""",
                        (lock_key, holder, now.isoformat(), expires),
                    )
                    return
            if time.monotonic() >= deadline:
                raise ToolLockTimeout(f"Timed out waiting for resource lock {lock_key!r}")
            time.sleep(0.05)

    def _release_lock(self, lock_key: str, holder: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM resource_locks WHERE lock_key = ? AND holder = ?",
                (lock_key, holder),
            )

    # ----------------------------------------------------------------- running

    def _run_handler(
        self, spec: ToolSpec, parameters: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], float | None]:
        """Run the handler under the tool's timeout contract (M1.1 §3).

        Read-only tools: executed on a worker thread; the CALLER gets its
        ToolExecutionTimeout at the declared deadline. The pool is shut down
        without joining (wait=False), so a stuck handler cannot hold the
        request hostage; an abandoned read-only thread cannot corrupt
        durable state.

        Side-effect tools: executed inline. An in-process thread cannot be
        cancelled safely mid-write, and pretending otherwise would risk a
        half-applied effect plus a retry. Adapters own their timeouts
        (network/process-level, kept <= spec.timeout_s); the gateway
        measures wall time and reports an overrun instead of lying about a
        cancellation. Returns (result, overrun_elapsed_s | None).
        """
        if not spec.side_effect:
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(spec.handler, parameters, idempotency_key)
            try:
                return future.result(timeout=spec.timeout_s), None
            except FutureTimeoutError as exc:
                raise ToolExecutionTimeout(
                    f"{spec.id} exceeded its {spec.timeout_s}s timeout"
                ) from exc
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        started = time.monotonic()
        result = spec.handler(parameters, idempotency_key)
        elapsed = time.monotonic() - started
        return result, (elapsed if elapsed > spec.timeout_s else None)

    def _mark_failed(
        self, run_id: str, spec: ToolSpec, idempotency_key: str, exc: Exception
    ) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE tool_executions SET state = 'FAILED', completed_at = ? "
                "WHERE idempotency_key = ?",
                (utc_now(), idempotency_key),
            )
            self.db._append_event_tx(
                conn,
                run_id,
                "tool.failed",
                {
                    "tool_id": spec.id,
                    "error": str(exc),
                    "error_class": type(exc).__name__,
                },
            )

    @staticmethod
    def _event_result(result: dict[str, Any]) -> dict[str, Any]:
        serialized = dumps(result)
        if len(serialized) <= EVENT_RESULT_LIMIT:
            return {"value": result}
        return {
            "truncated": True,
            "size": len(serialized),
            "preview": serialized[:500],
        }

    # ------------------------------------------------------------ car handlers

    def _read_history(self, parameters: dict[str, Any], _: str) -> dict[str, Any]:
        vehicle_id = str(parameters.get("vehicle_id", "genesis-2016"))
        event_type = str(parameters.get("event_type", "")) or None
        raw_types = parameters.get("event_types") or []
        event_types = tuple(str(item) for item in raw_types)
        if event_type and not event_types:
            event_types = (event_type,)
        return {
            "vehicle_id": vehicle_id,
            "event_types": list(event_types),
            "events": self.db.read_maintenance(vehicle_id, event_types=event_types),
        }

    def _log_maintenance(
        self, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        vehicle_id = str(parameters.get("vehicle_id", "genesis-2016"))
        event_type = str(parameters["event_type"])
        mileage = float(parameters["odometer_km"])
        notes = str(parameters.get("notes", ""))
        event_id = f"maint_{uuid.uuid4().hex}"
        occurred_at = str(parameters.get("occurred_at") or datetime.now(timezone.utc).date())

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM maintenance_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """INSERT INTO maintenance_events
                (id, vehicle_id, event_type, occurred_at, odometer_km, notes,
                 idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    vehicle_id,
                    event_type,
                    occurred_at,
                    mileage,
                    notes,
                    idempotency_key,
                    utc_now(),
                ),
            )
        return {
            "id": event_id,
            "vehicle_id": vehicle_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "odometer_km": mileage,
            "notes": notes,
        }
