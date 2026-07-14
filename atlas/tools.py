from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from atlas.db import Database, dumps, loads, utc_now


@dataclass(frozen=True)
class ToolSpec:
    id: str
    side_effect: bool
    handler: Callable[[dict[str, Any], str], dict[str, Any]]


class ToolGateway:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.tools: dict[str, ToolSpec] = {
            "car.read_history": ToolSpec("car.read_history", False, self._read_history),
            "car.log_maintenance": ToolSpec("car.log_maintenance", True, self._log_maintenance),
        }
        self.known_tools = set(self.tools)

    def execute(
        self,
        *,
        run_id: str,
        tool_id: str,
        parameters: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if tool_id not in self.tools:
            raise KeyError(f"Unknown tool: {tool_id}")
        spec = self.tools[tool_id]
        input_hash = hashlib.sha256(dumps(parameters).encode("utf-8")).hexdigest()

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["input_hash"] != input_hash:
                    raise ValueError("Idempotency key was reused with different input")
                if existing["state"] == "COMPLETED":
                    return loads(existing["result_json"], {})
                raise RuntimeError("A matching tool execution is already in progress")

            execution_id = f"tool_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO tool_executions
                (id, run_id, tool_id, idempotency_key, input_hash, state, created_at)
                VALUES (?, ?, ?, ?, ?, 'RUNNING', ?)""",
                (execution_id, run_id, tool_id, idempotency_key, input_hash, utc_now()),
            )
            self.db._append_event_tx(
                conn,
                run_id,
                "tool.started",
                {"execution_id": execution_id, "tool_id": tool_id},
            )

        try:
            result = spec.handler(parameters, idempotency_key)
        except Exception as exc:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE tool_executions SET state = 'FAILED', completed_at = ? WHERE idempotency_key = ?",
                    (utc_now(), idempotency_key),
                )
                self.db._append_event_tx(
                    conn,
                    run_id,
                    "tool.failed",
                    {"tool_id": tool_id, "error": str(exc)},
                )
            raise

        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE tool_executions
                SET state = 'COMPLETED', result_json = ?, completed_at = ?
                WHERE idempotency_key = ?""",
                (dumps(result), utc_now(), idempotency_key),
            )
            self.db._append_event_tx(
                conn,
                run_id,
                "tool.completed",
                {"tool_id": tool_id, "result": result},
            )
        return result

    def _read_history(self, parameters: dict[str, Any], _: str) -> dict[str, Any]:
        vehicle_id = str(parameters.get("vehicle_id", "genesis-2016"))
        return {"vehicle_id": vehicle_id, "events": self.db.read_maintenance(vehicle_id)}

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
