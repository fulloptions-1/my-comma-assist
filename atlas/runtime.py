from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from atlas.db import Database, dumps
from atlas.registry import AgentBlueprint, PackageRegistry
from atlas.tools import ToolGateway


TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}


class Runtime:
    def __init__(self, db: Database, packages_path: Path) -> None:
        self.db = db
        self.tools = ToolGateway(db)
        self.registry = PackageRegistry(packages_path, self.tools.known_tools)
        self.registry.load()

    def create_run(self, target_id: str, message: str) -> str:
        if target_id == "auto":
            target = self.registry.resolve(message)
        else:
            target = self.registry.get_agent(target_id)
        run_id = self.db.create_run(
            target_type="agent",
            target_id=target.id,
            target_version=target.version,
            input_data={"message": message},
        )
        self.db.transition(run_id, "RUNNING", event_type="run.started")
        self.advance(run_id)
        return run_id

    def advance(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if run["state"] in TERMINAL_STATES or run["state"].startswith("WAITING"):
            return
        blueprint = self.registry.get_agent(run["target_id"])
        try:
            if blueprint.handler == "concierge":
                self._run_concierge(run, blueprint)
            elif blueprint.handler == "car_maintenance":
                self._run_car(run, blueprint)
            else:
                raise ValueError(f"Unsupported handler: {blueprint.handler}")
        except Exception as exc:
            self.db.transition(
                run_id,
                "FAILED",
                output={"error": str(exc)},
                event_type="run.failed",
                payload={"error": str(exc)},
            )

    def answer(self, run_id: str, interaction_id: str, answer: Any) -> None:
        run = self.db.get_run(run_id)
        if run["state"] not in {"WAITING_FOR_USER", "WAITING_FOR_APPROVAL"}:
            raise ValueError("Run is not waiting for an interaction")
        interaction = self.db.resolve_interaction(interaction_id, answer)
        context = run["context"]

        if interaction["kind"] == "user_input":
            mileage = self._parse_mileage(str(answer))
            if mileage is None:
                raise ValueError("Enter mileage as a number in kilometres")
            context["odometer_km"] = mileage
            context["phase"] = "approval"
            self.db.transition(
                run_id,
                "RUNNING",
                context=context,
                event_type="run.resumed",
                payload={"source": "user_input"},
            )
            self._create_approval(run_id, context)
            return

        if interaction["kind"] == "approval":
            approved = bool(answer is True or str(answer).lower() in {"approve", "approved", "yes", "true"})
            proposed = interaction["payload"].get("proposed_action", {})
            expected_hash = hashlib.sha256(dumps(proposed).encode("utf-8")).hexdigest()
            if expected_hash != interaction["artifact_hash"]:
                raise ValueError("Approval artifact changed")
            if not approved:
                self.db.transition(
                    run_id,
                    "CANCELLED",
                    output={"message": "Action rejected"},
                    event_type="run.cancelled",
                )
                return
            self.db.transition(
                run_id,
                "RUNNING",
                context=context,
                event_type="run.resumed",
                payload={"source": "approval"},
            )
            self._execute_maintenance_write(run_id, proposed)
            return

        raise ValueError(f"Unsupported interaction kind: {interaction['kind']}")

    def _run_concierge(self, run: dict[str, Any], _: AgentBlueprint) -> None:
        message = str(run["input"].get("message", ""))
        resolved = self.registry.resolve(message)
        if resolved.id == "concierge-agent":
            self.db.transition(
                run["id"],
                "COMPLETED",
                output={
                    "message": "I could not confidently choose a capability yet.",
                    "available_agents": sorted(self.registry.agents),
                },
                event_type="run.completed",
            )
            return
        self.db.append_event(
            run["id"],
            "capability.resolved",
            {"target_id": resolved.id, "target_version": resolved.version},
        )
        self.db.transition(
            run["id"],
            "RUNNING",
            context={"delegated_to": resolved.id},
            event_type="agent.delegated",
            payload={"target_id": resolved.id},
        )
        # In this first vertical slice the delegated handler executes inside the
        # same durable run. Later child-run support can replace this without
        # changing the external API or event model.
        fresh = self.db.get_run(run["id"])
        if resolved.handler == "car_maintenance":
            self._run_car(fresh, resolved)
        else:
            raise ValueError(f"Unsupported delegated handler: {resolved.handler}")

    def _run_car(self, run: dict[str, Any], _: AgentBlueprint) -> None:
        message = str(run["input"].get("message", ""))
        normalized = message.lower()
        context = run["context"]
        self.db.append_event(run["id"], "agent.started", {"agent_id": "car-maintenance-agent"})

        if any(word in normalized for word in ("when", "history", "last")):
            result = self.tools.execute(
                run_id=run["id"],
                tool_id="car.read_history",
                parameters={"vehicle_id": "genesis-2016"},
                idempotency_key=f"{run['id']}:read-history",
            )
            events = result["events"]
            if not events:
                message_out = "No maintenance events are logged yet."
            else:
                latest = events[0]
                message_out = (
                    f"Latest: {latest['event_type'].replace('_', ' ')} at "
                    f"{latest['odometer_km']:,.0f} km on {latest['occurred_at']}."
                )
            self.db.transition(
                run["id"],
                "COMPLETED",
                output={"message": message_out, "events": events},
                event_type="run.completed",
            )
            return

        event_type = self._detect_event_type(normalized)
        mileage = self._parse_mileage(message)
        context.update(
            {
                "phase": "collect_mileage" if mileage is None else "approval",
                "vehicle_id": "genesis-2016",
                "event_type": event_type,
            }
        )
        if mileage is not None:
            context["odometer_km"] = mileage
            self.db.transition(
                run["id"], "RUNNING", context=context, event_type="agent.context_updated"
            )
            self._create_approval(run["id"], context)
            return

        self.db.transition(
            run["id"],
            "WAITING_FOR_USER",
            context=context,
            event_type="run.waiting_for_user",
        )
        self.db.create_interaction(
            run_id=run["id"],
            kind="user_input",
            prompt="What is the current dashboard mileage in kilometres?",
            payload={"expected": "number", "unit": "km"},
        )

    def _create_approval(self, run_id: str, context: dict[str, Any]) -> None:
        proposed = {
            "vehicle_id": context["vehicle_id"],
            "event_type": context["event_type"],
            "odometer_km": context["odometer_km"],
        }
        artifact_hash = hashlib.sha256(dumps(proposed).encode("utf-8")).hexdigest()
        self.db.transition(
            run_id,
            "WAITING_FOR_APPROVAL",
            context=context,
            event_type="run.waiting_for_approval",
        )
        self.db.create_interaction(
            run_id=run_id,
            kind="approval",
            prompt=(
                f"Log {context['event_type'].replace('_', ' ')} for the 2016 Genesis "
                f"at {context['odometer_km']:,.0f} km?"
            ),
            payload={"proposed_action": proposed},
            artifact_hash=artifact_hash,
        )

    def _execute_maintenance_write(self, run_id: str, proposed: dict[str, Any]) -> None:
        result = self.tools.execute(
            run_id=run_id,
            tool_id="car.log_maintenance",
            parameters=proposed,
            idempotency_key=f"{run_id}:log-maintenance",
        )
        self.db.transition(
            run_id,
            "COMPLETED",
            output={
                "message": (
                    f"Logged {result['event_type'].replace('_', ' ')} at "
                    f"{result['odometer_km']:,.0f} km."
                ),
                "maintenance_event": result,
            },
            event_type="run.completed",
        )

    @staticmethod
    def _parse_mileage(text: str) -> float | None:
        matches = re.findall(r"(?<!\d)(\d{1,3}(?:[ ,]\d{3})+|\d{4,7})(?:\.\d+)?", text)
        if not matches:
            return None
        value = float(matches[-1].replace(",", "").replace(" ", ""))
        if value < 100 or value > 5_000_000:
            return None
        return value

    @staticmethod
    def _detect_event_type(text: str) -> str:
        if "tire" in text or "tyre" in text:
            return "tire_replacement"
        if "insurance" in text:
            return "insurance_renewal"
        if "filter" in text and "oil" in text:
            return "oil_and_filter_change"
        if "oil" in text:
            return "oil_change"
        return "maintenance_service"
