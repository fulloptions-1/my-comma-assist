from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from atlas.db import (
    Database,
    TERMINAL_STATES,
    WAITING_STATES,
    dumps,
)
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from atlas.profiles import ModelProfile, ProfileStore
from atlas.providers import (
    AnthropicProvider,
    FakeProvider,
    Provider,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ToolDef,
)
from atlas.registry import AgentBlueprint, PackageRegistry, PackageValidationError
from atlas.tools import ToolGateway, ToolInputError, ToolPermissionError

LLM_DEFAULT_MODEL_CALLS = 8
LLM_DEFAULT_TOOL_CALLS = 16
LLM_AGENT_FIELDS = {
    "system_prompt",
    "provider",
    "model",
    "max_tokens",
    "max_model_calls",
    "max_tool_calls",
    "profile",
}
_UNSET = object()


@dataclass(frozen=True)
class LlmSettings:
    """Effective llm-run configuration: agent fields > profile > defaults."""

    provider: str
    model: str
    max_tokens: int
    timeout_s: float | None
    max_model_calls: int
    max_tool_calls: int
    input_cost_per_mtok: float | None
    output_cost_per_mtok: float | None
    cost_budget_usd: float | None
    fallback: str | None
USER_ASK_TOOL = ToolDef(
    name="user.ask",
    description="Ask the human user one question and wait for their reply.",
    schema={"question": {"type": "string", "required": True}},
)


class Runtime:
    def __init__(
        self,
        db: Database,
        packages_path: Path,
        providers: dict[str, Provider] | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.db = db
        self.tools = ToolGateway(db)
        self.profiles = ProfileStore(db)
        # Instance-local secret configuration (M1.3 §4): an isolated app
        # factory must not depend on the process environment to activate
        # the providers whose keys it just stored.
        self.secret_key = (
            secret_key if secret_key is not None
            else os.environ.get("ATLAS_SECRET_KEY", "")
        )
        self.providers: dict[str, Provider] = (
            providers if providers is not None
            else self._default_providers(db, self.secret_key)
        )
        self.registry = PackageRegistry(
            packages_path,
            self.tools.known_tools,
            known_handlers={"concierge", "car_maintenance", "llm"},
            extra_agent_fields=LLM_AGENT_FIELDS,
        )
        self.registry.load()
        self._validate_llm_blueprints()
        self.recover()

    @staticmethod
    def _default_providers(db: Database, secret_key: str) -> dict[str, Provider]:
        """fake is always available (keyless). anthropic joins only when both
        a secret key and an encrypted API key are configured; the decrypted
        key is passed straight into the adapter and nowhere else. The key is
        a PARAMETER (M1.3 §4) — this function never reads the environment."""
        providers: dict[str, Provider] = {"fake": FakeProvider()}
        if secret_key:
            from atlas.secrets import (
                ANTHROPIC_KEY_SETTING,
                OPENAI_COMPAT_KEY_SETTING,
                SecretStore,
            )

            store = SecretStore(db, key=secret_key)
            anthropic_key = store.get(ANTHROPIC_KEY_SETTING)
            if anthropic_key:
                providers["anthropic"] = AnthropicProvider(anthropic_key)
            openai_conf = db.get_config("providers.openai_compat", {})
            openai_key = store.get(OPENAI_COMPAT_KEY_SETTING)
            if openai_key and openai_conf.get("base_url"):
                from atlas.providers import OpenAICompatProvider

                providers["openai_compat"] = OpenAICompatProvider(
                    openai_key,
                    base_url=str(openai_conf["base_url"]),
                    model=str(openai_conf.get("model", "")),
                    timeout=float(openai_conf.get("timeout_s", 60.0)),
                )
        return providers

    def _validate_llm_blueprints(self) -> None:
        """Side-effect tools inside the model loop require approval support
        that does not exist yet; fail loudly at load instead of at run time."""
        problems: list[str] = []
        for blueprint in self.registry.agents.values():
            if blueprint.handler != "llm":
                continue
            side_effect = [
                tool_id
                for tool_id in blueprint.tools
                if self.tools.tools[tool_id].side_effect
            ]
            if side_effect:
                problems.append(
                    f"{blueprint.id}: llm agents cannot declare side-effect tools "
                    f"yet (approval-in-loop is not implemented): {', '.join(side_effect)}"
                )
            profile_id = blueprint.field("profile")
            if profile_id is not None and self.profiles.get(str(profile_id)) is None:
                problems.append(
                    f"{blueprint.id}: unknown model profile {profile_id!r} "
                    f"(available: {', '.join(sorted(self.profiles.list()))})"
                )
        if problems:
            raise PackageValidationError("\n".join(problems))

    # ------------------------------------------------------------------ runs

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
            context={"definition_fingerprint": target.fingerprint},
        )
        self.db.transition(
            run_id, "RUNNING", event_type="run.started", expected_state="CREATED"
        )
        self.advance(run_id)
        return run_id

    def _pin_effective_agent(
        self, context: dict[str, Any], blueprint: AgentBlueprint
    ) -> None:
        """Snapshot the agent that is ACTUALLY executing this run (M1.2 §1).

        run.target_id stays the user's original entry target (e.g. the
        concierge); effective_agent is who does the work after delegation.
        The snapshot is durable, so resumes and restarts continue with the
        same identity, prompt, and tool permissions."""
        context["effective_agent"] = {
            "id": blueprint.id,
            "version": blueprint.version,
            "fingerprint": blueprint.fingerprint,
            "handler": blueprint.handler,
            "system_prompt": blueprint.field("system_prompt"),
            "tools": list(blueprint.tools),
        }

    def _effective_blueprint(self, run: dict[str, Any]) -> AgentBlueprint:
        """The pinned executing agent, verified against loaded definitions.

        A fingerprint/version mismatch means the YAML changed while the run
        was in flight; that fails loudly instead of silently resuming with a
        different prompt or tool set (M1.2 §1)."""
        pinned = run["context"].get("effective_agent")
        agent_id = pinned["id"] if pinned else run["target_id"]
        blueprint = self.registry.get_agent(agent_id)
        if pinned and (
            blueprint.fingerprint != pinned["fingerprint"]
            or blueprint.version != pinned["version"]
        ):
            raise RuntimeError(
                f"Agent definition for {agent_id!r} changed while the run was "
                f"in flight (pinned v{pinned['version']} "
                f"{pinned['fingerprint'][:12]}…, loaded v{blueprint.version} "
                f"{blueprint.fingerprint[:12]}…); refusing to resume silently"
            )
        return blueprint

    def advance(self, run_id: str) -> None:
        run = self.db.get_run(run_id)
        if run["state"] in TERMINAL_STATES or run["state"] in WAITING_STATES:
            return
        blueprint = self.registry.get_agent(run["target_id"])
        if "effective_agent" not in run["context"]:
            self._pin_effective_agent(run["context"], blueprint)
        try:
            if blueprint.handler == "concierge":
                self._run_concierge(run, blueprint)
            elif blueprint.handler == "car_maintenance":
                self._run_car(run, blueprint)
            elif blueprint.handler == "llm":
                self._run_llm(run, blueprint)
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

    # --------------------------------------------------------------- recovery

    def recover(self) -> dict[str, int]:
        """Startup recovery. Safe to run repeatedly; touches only defective runs.

        This engine advances runs synchronously inside requests, so any run
        found CREATED or RUNNING at boot was in flight when the previous
        process died. Those are failed loudly rather than left as zombies.
        Waiting runs that lost their interaction (historical F-01/F-07 wedges)
        get the interaction re-issued deterministically from run context.
        """
        stats = {
            "orphaned_failed": 0,
            "waiting_repaired": 0,
            "waiting_unrepairable": 0,
            "continuations_resumed": 0,
        }

        # FIRST reclassify tool claims held by the dead previous process, so
        # approval resumes below can retry the same idempotency key instead of
        # colliding with a fresh-looking RUNNING row (M1.2 §2).
        stats["executions_reclaimed"] = self.tools.reclaim_orphaned_executions()

        for run in self.db.runs_in_states({"CREATED", "RUNNING"}):
            if run["state"] == "RUNNING" and run["context"].get("continuation"):
                try:
                    self.db.append_event(
                        run["id"],
                        "run.continuation_resumed",
                        {"kind": run["context"]["continuation"].get("kind")},
                    )
                    self._resume_continuation(run)
                    stats["continuations_resumed"] += 1
                    continue
                except Exception as exc:
                    self.db.transition(
                        run["id"],
                        "FAILED",
                        output={"error": f"Continuation resume failed: {exc}"},
                        event_type="run.recovered_orphan",
                        payload={"previous_state": "RUNNING", "resume_error": str(exc)},
                        expected_state="RUNNING",
                    )
                    stats["orphaned_failed"] += 1
                    continue
            committed = run["context"].get("executing_approval")
            if run["state"] == "RUNNING" and committed:
                # The user approved and the exact action was durably pinned:
                # finishing the work is the correct recovery, not failing.
                # The pinned idempotency key makes this exactly-once — if
                # the side effect already happened, the gateway replays its
                # original result (M1.1 §5).
                try:
                    self.db.append_event(
                        run["id"],
                        "run.approval_resumed",
                        {"idempotency_key": committed["idempotency_key"]},
                    )
                    self._execute_maintenance_write(
                        run["id"], committed["proposed_action"]
                    )
                    stats["approvals_resumed"] = stats.get("approvals_resumed", 0) + 1
                    continue
                except Exception as exc:
                    self.db.transition(
                        run["id"],
                        "FAILED",
                        output={"error": f"Approval resume failed: {exc}"},
                        event_type="run.recovered_orphan",
                        payload={"previous_state": run["state"], "resume_error": str(exc)},
                        expected_state="RUNNING",
                    )
                    stats["orphaned_failed"] += 1
                    continue
            self.db.transition(
                run["id"],
                "FAILED",
                output={"error": "Recovered orphaned run after process restart"},
                event_type="run.recovered_orphan",
                payload={"previous_state": run["state"]},
                expected_state=run["state"],
            )
            stats["orphaned_failed"] += 1

        for run in self.db.runs_in_states(WAITING_STATES):
            if self.db.pending_interaction(run["id"]) is not None:
                continue  # healthy waiting run
            if self._reissue_interaction(run):
                stats["waiting_repaired"] += 1
            else:
                self.db.transition(
                    run["id"],
                    "FAILED",
                    output={"error": "Waiting run lost its interaction and could not be repaired"},
                    event_type="run.repair_failed",
                    expected_state=run["state"],
                )
                stats["waiting_unrepairable"] += 1

        self.tools.recover_stale_executions()
        return stats

    def _reissue_interaction(self, run: dict[str, Any]) -> bool:
        context = run["context"]
        phase = context.get("phase")
        if run["state"] == "WAITING_FOR_USER" and context.get("concierge_options"):
            self._ask_concierge_choice_reissue(run["id"], context["concierge_options"])
            self.db.append_event(run["id"], "run.repaired", {"reissued": "capability_choice"})
            return True
        if run["state"] == "WAITING_FOR_USER" and phase == "collect_mileage":
            self.db.create_interaction(
                run_id=run["id"],
                kind="user_input",
                prompt="What is the current dashboard mileage in kilometres?",
                payload={"expected": "number", "unit": "km", "resume": "car_mileage"},
            )
            self.db.append_event(run["id"], "run.repaired", {"reissued": "user_input"})
            return True
        if (
            run["state"] == "WAITING_FOR_APPROVAL"
            and phase == "approval"
            and all(k in context for k in ("vehicle_id", "event_type", "odometer_km"))
        ):
            proposed = self._proposed_action(context)
            self.db.create_interaction(
                run_id=run["id"],
                kind="approval",
                prompt=self._approval_prompt(context),
                payload={"proposed_action": proposed, "resume": "car_approval"},
                artifact_hash=self._hash_payload(proposed),
            )
            self.db.append_event(run["id"], "run.repaired", {"reissued": "approval"})
            return True
        return False

    # ------------------------------------------------------------ interactions

    def answer(self, run_id: str, interaction_id: str, answer: Any) -> None:
        run = self.db.get_run(run_id)
        state = run["state"]
        if state not in WAITING_STATES:
            raise ValueError("Run is not waiting for an interaction")

        interaction = self.db.get_interaction(interaction_id)
        if interaction["run_id"] != run_id:
            raise ValueError("Interaction does not belong to this run")
        if interaction["state"] != "PENDING":
            raise ValueError("Interaction is already resolved")
        expected_kind = "user_input" if state == "WAITING_FOR_USER" else "approval"
        if interaction["kind"] != expected_kind:
            raise ValueError(
                f"Run is waiting for {expected_kind!r}, not {interaction['kind']!r}"
            )

        if interaction["payload"].get("resume") == "llm":
            self._answer_llm(run, interaction, answer)
            return
        if interaction["payload"].get("resume") == "concierge_choice":
            self._answer_concierge_choice(run, interaction, answer)
            return
        if expected_kind == "user_input":
            self._answer_car_mileage(run, interaction, answer)
            return
        self._answer_car_approval(run, interaction, answer)

    def _answer_car_mileage(
        self, run: dict[str, Any], interaction: dict[str, Any], answer: Any
    ) -> None:
        # Validate BEFORE resolving: an unparsable answer must leave the
        # interaction PENDING so the user can simply try again (fixes F-01).
        mileage = self._parse_mileage(str(answer))
        if mileage is None:
            raise ValueError("Enter mileage as a number in kilometres")
        context = run["context"]
        context["odometer_km"] = mileage
        context["phase"] = "approval"
        context.setdefault(
            "occurred_at", datetime.now(timezone.utc).date().isoformat()
        )
        proposed = self._proposed_action(context)
        # Answer, context, state, the approval question, and every event
        # commit in ONE transaction — there is no RUNNING hop between the
        # mileage answer and the approval for a crash to orphan (M1.2 §4).
        self.db.resolve_and_requeue_interaction(
            interaction["id"],
            answer,
            run_id=run["id"],
            expected_kind="user_input",
            new_state="WAITING_FOR_APPROVAL",
            next_kind="approval",
            next_prompt=self._approval_prompt(context),
            next_payload={"proposed_action": proposed, "resume": "car_approval"},
            next_artifact_hash=self._hash_payload(proposed),
            context=context,
            event_type="run.waiting",
            payload={"reason": "approval", "via": "user_input"},
            expected_state="WAITING_FOR_USER",
        )

    def _answer_car_approval(
        self, run: dict[str, Any], interaction: dict[str, Any], answer: Any
    ) -> None:
        proposed = interaction["payload"].get("proposed_action", {})
        # Validate the approval binding BEFORE resolving: a hash mismatch means
        # the stored payload changed after approval was requested — keep the
        # interaction PENDING as evidence and refuse to act on it.
        if self._hash_payload(proposed) != interaction["artifact_hash"]:
            raise ValueError("Approval artifact changed")
        approved = self._commit_car_approval(run, interaction, answer, proposed)
        if approved:
            self._execute_maintenance_write(run["id"], proposed)

    def _commit_car_approval(
        self,
        run: dict[str, Any],
        interaction: dict[str, Any],
        answer: Any,
        proposed: dict[str, Any],
    ) -> bool:
        """Durably record the approval decision in ONE transaction (M1.1 §5).

        On approve, the exact proposed action and its deterministic
        idempotency key are pinned into context as `executing_approval`
        together with the RUNNING transition, so a crash at ANY later point
        is recoverable: startup recovery re-executes with the same key and
        the gateway either performs the effect once or replays its result.
        """
        approved = bool(
            answer is True or str(answer).lower() in {"approve", "approved", "yes", "true"}
        )
        if not approved:
            self.db.resolve_interaction_and_transition(
                interaction["id"],
                answer,
                run_id=run["id"],
                expected_kind="approval",
                new_state="CANCELLED",
                output={"message": "Action rejected"},
                event_type="run.cancelled",
                expected_state="WAITING_FOR_APPROVAL",
            )
            return False
        context = run["context"]
        context["executing_approval"] = {
            "interaction_id": interaction["id"],
            "proposed_action": proposed,
            "idempotency_key": f"{run['id']}:log-maintenance",
        }
        self.db.resolve_interaction_and_transition(
            interaction["id"],
            answer,
            run_id=run["id"],
            expected_kind="approval",
            new_state="RUNNING",
            context=context,
            event_type="run.resumed",
            payload={"source": "approval", "executing_approval": True},
            expected_state="WAITING_FOR_APPROVAL",
        )
        return True

    # ----------------------------------------------------------- car handlers

    GENERAL_ASSISTANT_ID = "assistant-agent"

    def _run_concierge(self, run: dict[str, Any], _: AgentBlueprint) -> None:
        message = str(run["input"].get("message", ""))
        scored = self.registry.match(message)
        if not scored:
            # No specialist matched: general conversation goes to the
            # assistant instead of a dead-end "could not choose" (M1.1 §2).
            if self.GENERAL_ASSISTANT_ID in self.registry.agents:
                self._delegate(run, self.registry.get_agent(self.GENERAL_ASSISTANT_ID))
                return
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
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            # Task-like but ambiguous: ask instead of guessing. State change
            # and question are one transaction (the S1 atomic-wait rule).
            options = [agent.id for score, agent in scored if score == scored[0][0]]
            context = run["context"]
            context["concierge_options"] = options
            self.db.transition_with_interaction(
                run["id"],
                "WAITING_FOR_USER",
                kind="user_input",
                prompt="Multiple capabilities match. Which one should handle this? "
                + ", ".join(options),
                payload={"options": options, "resume": "concierge_choice"},
                context=context,
                expected_state="RUNNING",
            )
            return
        self._delegate(run, scored[0][1])

    def _ask_concierge_choice_reissue(self, run_id: str, options: list[str]) -> None:
        self.db.create_interaction(
            run_id=run_id,
            kind="user_input",
            prompt="Multiple capabilities match. Which one should handle this? "
            + ", ".join(options),
            payload={"options": options, "resume": "concierge_choice"},
        )

    def _answer_concierge_choice(
        self, run: dict[str, Any], interaction: dict[str, Any], answer: Any
    ) -> None:
        options = list(interaction["payload"].get("options", []))
        choice = str(answer).strip()
        # Validate BEFORE resolving so a wrong choice stays answerable.
        if choice not in options:
            raise ValueError(f"Choose one of: {', '.join(options)}")
        context = run["context"]
        context["continuation"] = {"kind": "delegate", "target_id": choice}
        # Decision and continuation phase commit atomically (M1.2 §4); a
        # crash before delegation is resumed at startup from the phase.
        self.db.resolve_interaction_and_transition(
            interaction["id"],
            answer,
            run_id=run["id"],
            expected_kind="user_input",
            new_state="RUNNING",
            context=context,
            event_type="run.resumed",
            payload={"source": "capability_choice"},
            expected_state="WAITING_FOR_USER",
        )
        self._resume_continuation(self.db.get_run(run["id"]))

    def _resume_continuation(self, run: dict[str, Any]) -> None:
        """Execute the durable continuation committed with an answer (M1.2 §4).

        Called inline right after the atomic commit and again by startup
        recovery for RUNNING runs that crashed in between. Model calls are
        at-least-once across a crash at this exact boundary (a provider call
        is not idempotent); everything durable remains exactly-once."""
        context = run["context"]
        continuation = context.get("continuation") or {}
        kind = continuation.get("kind")
        if kind == "delegate":
            target = str(continuation.get("target_id", ""))
            context.pop("continuation", None)
            self._delegate(run, self.registry.get_agent(target))
            return
        if kind == "llm":
            blueprint = self._effective_blueprint(run)
            context.pop("continuation", None)
            try:
                self._llm_loop(run["id"], blueprint, context)
            except Exception as exc:
                self.db.transition(
                    run["id"],
                    "FAILED",
                    output={"error": str(exc)},
                    event_type="run.failed",
                    payload={"error": str(exc)},
                )
            return
        raise ValueError(f"Unknown continuation kind: {kind!r}")

    def _delegate(self, run: dict[str, Any], resolved: AgentBlueprint) -> None:
        self.db.append_event(
            run["id"],
            "capability.resolved",
            {"target_id": resolved.id, "target_version": resolved.version},
        )
        # MERGE into the live context (the M1.1 version replaced the whole
        # context with {"delegated_to": ...}, destroying pinned state — §1)
        # and pin the delegated agent as the effective executor.
        context = run["context"]
        context["delegated_to"] = resolved.id
        self._pin_effective_agent(context, resolved)
        self.db.update_context(
            run["id"],
            context,
            event_type="agent.delegated",
            payload={"target_id": resolved.id},
        )
        # In this first vertical slice the delegated handler executes inside the
        # same durable run. Later child-run support can replace this without
        # changing the external API or event model.
        fresh = self.db.get_run(run["id"])
        if resolved.handler == "car_maintenance":
            self._run_car(fresh, resolved)
        elif resolved.handler == "llm":
            self._run_llm(fresh, resolved)
        else:
            raise ValueError(f"Unsupported delegated handler: {resolved.handler}")

    def _run_car(self, run: dict[str, Any], blueprint: AgentBlueprint) -> None:
        message = str(run["input"].get("message", ""))
        normalized = message.lower()
        context = run["context"]
        self.db.append_event(run["id"], "agent.started", {"agent_id": blueprint.id})

        if any(word in normalized for word in ("when", "history", "last")):
            # Event-specific questions filter; generic history stays
            # unfiltered (M1.4 §4): "last oil change" must never return
            # the tire job that merely happened most recently.
            wanted = self._match_event_types(normalized)
            parameters: dict[str, Any] = {"vehicle_id": "genesis-2016"}
            if wanted:
                parameters["event_types"] = list(wanted)
            result = self.tools.execute(
                run_id=run["id"],
                tool_id="car.read_history",
                parameters=parameters,
                idempotency_key=f"{run['id']}:read-history",
                allowed_tools=blueprint.tools,
            )
            events = result["events"]
            if not events:
                if wanted and len(wanted) > 1:
                    label = "oil change"  # the only multi-type category
                elif wanted:
                    label = wanted[0].replace("_", " ")
                else:
                    label = "maintenance"
                message_out = f"No {label} events are logged yet."
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
                "allowed_tools": list(blueprint.tools),
            }
        )
        if mileage is not None:
            # setdefault: a reissued approval after recovery keeps the
            # originally pinned date (M1.5 §1).
            context.setdefault(
                "occurred_at", datetime.now(timezone.utc).date().isoformat()
            )
        if mileage is not None:
            context["odometer_km"] = mileage
            self.db.update_context(run["id"], context, event_type="agent.context_updated")
            self._create_approval(run["id"], context)
            return

        # Waiting state and its interaction commit atomically (fixes F-07).
        self.db.transition_with_interaction(
            run["id"],
            "WAITING_FOR_USER",
            kind="user_input",
            prompt="What is the current dashboard mileage in kilometres?",
            payload={"expected": "number", "unit": "km", "resume": "car_mileage"},
            context=context,
            expected_state="RUNNING",
        )

    def _create_approval(self, run_id: str, context: dict[str, Any]) -> None:
        proposed = self._proposed_action(context)
        self.db.transition_with_interaction(
            run_id,
            "WAITING_FOR_APPROVAL",
            kind="approval",
            prompt=self._approval_prompt(context),
            payload={"proposed_action": proposed, "resume": "car_approval"},
            artifact_hash=self._hash_payload(proposed),
            context=context,
            expected_state="RUNNING",
        )

    def _execute_maintenance_write(self, run_id: str, proposed: dict[str, Any]) -> None:
        allowed = tuple(self.db.get_run(run_id)["context"].get("allowed_tools", ()))
        result = self.tools.execute(
            run_id=run_id,
            tool_id="car.log_maintenance",
            parameters=proposed,
            idempotency_key=f"{run_id}:log-maintenance",
            allowed_tools=allowed or ("car.log_maintenance",),
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

    # ------------------------------------------------------------ llm handler

    def _provider_for(self, name: str) -> Provider:
        provider = self.providers.get(name)
        if provider is None:
            raise ValueError(
                f"Unknown provider {name!r} (configured: {', '.join(sorted(self.providers))})"
            )
        return provider

    def _llm_settings(
        self, blueprint: AgentBlueprint, context: dict[str, Any]
    ) -> LlmSettings:
        pinned = context.get("llm_settings")
        if pinned:
            # Snapshot taken when the run first executed: a paused run keeps
            # the exact configuration it started with, across restarts and
            # active-profile changes (M1.1 §1).
            return LlmSettings(**pinned)
        profile: ModelProfile | None = None
        profile_id = blueprint.field("profile")
        if profile_id is None and blueprint.field("provider") is None:
            # Agent declares nothing: follow the user's active profile.
            profile_id = self.profiles.active_profile_id()
        if profile_id is not None:
            profile = self.profiles.get(str(profile_id))
            if profile is None:
                raise ValueError(f"Unknown model profile: {profile_id}")

        def pick(field_name: str | None, attr: str, default: Any) -> Any:
            if field_name is not None:
                value = blueprint.field(field_name, _UNSET)
                if value is not _UNSET:
                    return value
            if profile is not None:
                value = getattr(profile, attr)
                if value is not None:
                    return value
            return default

        return LlmSettings(
            provider=str(pick("provider", "provider", "fake")),
            model=str(pick("model", "model", "")),
            max_tokens=int(pick("max_tokens", "max_tokens", 1024)),
            timeout_s=pick(None, "timeout_s", None),
            max_model_calls=int(
                pick("max_model_calls", "max_model_calls", LLM_DEFAULT_MODEL_CALLS)
            ),
            max_tool_calls=int(
                pick("max_tool_calls", "max_tool_calls", LLM_DEFAULT_TOOL_CALLS)
            ),
            input_cost_per_mtok=pick(None, "input_cost_per_mtok", None),
            output_cost_per_mtok=pick(None, "output_cost_per_mtok", None),
            cost_budget_usd=pick(None, "cost_budget_usd", None),
            fallback=pick(None, "fallback", None),
        )

    def _complete_with_fallback(
        self,
        provider: Provider,
        request: ProviderRequest,
        settings: LlmSettings,
        run_id: str,
        context: dict[str, Any],
        remaining_budget: int,
    ) -> tuple[Any, Provider, tuple[float | None, float | None]]:
        """One provider call with at most ONE fallback hop on retryable errors.

        Owns attempt accounting (M1.2 §3): every attempted provider call —
        including failed primaries — increments context['model_calls'] and
        appends a durable agent.provider_attempt event. A fallback hop is
        only made when the pinned budget has room for BOTH attempts; with one
        attempt remaining, a failed primary fails the run instead of
        overspending. Failure paths persist the attempt count before raising
        so a resumed run cannot reset it.

        Returns (result, provider_used, (in_price, out_price)); prices come
        from whichever profile actually answered (M1.1 §8), and the fallback
        profile snapshot stays pinned in run context."""
        primary_pricing = (settings.input_cost_per_mtok, settings.output_cost_per_mtok)
        attempt_no = context["model_calls"] + 1
        try:
            result = provider.complete(request)
            context["model_calls"] += 1
            self.db.append_event(
                run_id,
                "agent.provider_attempt",
                {"attempt": attempt_no, "provider": provider.id, "ok": True},
            )
            return result, provider, primary_pricing
        except ProviderError as exc:
            context["model_calls"] += 1
            self.db.append_event(
                run_id,
                "agent.provider_attempt",
                {
                    "attempt": attempt_no,
                    "provider": provider.id,
                    "ok": False,
                    "category": exc.category,
                },
            )
            if not (exc.retryable and settings.fallback):
                self.db.update_context(run_id, context)  # durable count
                raise
            if remaining_budget < 2:
                self.db.update_context(run_id, context)
                raise RuntimeError(
                    f"Budget exhausted: the primary call failed ({exc.category}) "
                    f"and no budget remains for the fallback "
                    f"({settings.max_model_calls} model calls)"
                ) from exc
            snapshot = context.get("fallback_profile")
            if snapshot:
                # Pinned at run start (M1.5 §3): edits or deletion of the
                # live profile never alter an in-flight run.
                fallback: ModelProfile | None = ModelProfile.from_dict(snapshot)
            else:
                # Runs started before M1.5 pinned only on first use.
                fallback = self.profiles.get(settings.fallback)
                if fallback is not None:
                    context["fallback_profile"] = fallback.to_dict()
            if fallback is None:
                raise
            fallback_provider = self.providers.get(fallback.provider)
            if fallback_provider is None:
                raise ProviderError(
                    exc.category,
                    f"{exc}; fallback profile {fallback.id!r} needs "
                    f"unconfigured provider {fallback.provider!r}",
                    retryable=exc.retryable,
                ) from exc
            self.db.append_event(
                run_id,
                "agent.fallback_used",
                {
                    "category": exc.category,
                    "to_profile": fallback.id,
                    "to_provider": fallback.provider,
                    "attempts": 2,
                },
            )
            fallback_request = replace(
                request,
                model=fallback.model or request.model,
                max_tokens=fallback.max_tokens,
                timeout_s=fallback.timeout_s,
            )
            fb_attempt = context["model_calls"] + 1
            try:
                fb_result = fallback_provider.complete(fallback_request)
            except ProviderError as fb_exc:
                context["model_calls"] += 1
                self.db.append_event(
                    run_id,
                    "agent.provider_attempt",
                    {
                        "attempt": fb_attempt,
                        "provider": fallback_provider.id,
                        "ok": False,
                        "category": fb_exc.category,
                    },
                )
                self.db.update_context(run_id, context)  # two attempts, durably
                raise
            context["model_calls"] += 1
            self.db.append_event(
                run_id,
                "agent.provider_attempt",
                {"attempt": fb_attempt, "provider": fallback_provider.id, "ok": True},
            )
            return (
                fb_result,
                fallback_provider,
                (fallback.input_cost_per_mtok, fallback.output_cost_per_mtok),
            )

    def _tool_defs_for(self, blueprint: AgentBlueprint) -> tuple[ToolDef, ...]:
        return self.tools.tool_defs(blueprint.tools) + (USER_ASK_TOOL,)

    def _run_llm(self, run: dict[str, Any], blueprint: AgentBlueprint) -> None:
        context = run["context"]
        if "messages" not in context:
            context.update(
                {
                    "messages": [
                        ProviderMessage(
                            "user", str(run["input"].get("message", ""))
                        ).to_dict()
                    ],
                    "model_calls": 0,
                    "tool_call_seq": 0,
                    "phase": "llm",
                    "allowed_tools": list(blueprint.tools),
                }
            )
            self.db.append_event(run["id"], "agent.started", {"agent_id": blueprint.id})
        self._llm_loop(run["id"], blueprint, context)

    def _llm_loop(
        self, run_id: str, blueprint: AgentBlueprint, context: dict[str, Any]
    ) -> None:
        """The generic agent loop. Deterministic code is in charge: the model
        proposes tool calls and text; the kernel owns budgets, permissions,
        idempotency, durable waits, and completion."""
        settings = self._llm_settings(blueprint, context)
        if "llm_settings" not in context:
            context["llm_settings"] = asdict(settings)  # pin for the whole run
            if settings.fallback:
                # Pin the FALLBACK profile at run start too (M1.5 §3):
                # loading it lazily on first primary failure let an edit to
                # the live profile change a paused run's behavior. Missing
                # or chained fallbacks fail here, before any provider call.
                pinned_fallback = self.profiles.get(settings.fallback)
                if pinned_fallback is None:
                    raise RuntimeError(
                        f"Fallback profile not found at run start: "
                        f"{settings.fallback!r}"
                    )
                if pinned_fallback.fallback is not None:
                    raise RuntimeError(
                        "Fallback chains are not allowed (one hop only): "
                        f"{settings.fallback!r} declares its own fallback"
                    )
                context["fallback_profile"] = pinned_fallback.to_dict()
        provider = self._provider_for(settings.provider)
        max_model = settings.max_model_calls
        max_tools = settings.max_tool_calls
        tool_defs = self._tool_defs_for(blueprint)

        while True:
            if context["model_calls"] >= max_model:
                raise RuntimeError(f"Budget exhausted: {max_model} model calls")
            request = ProviderRequest(
                messages=tuple(
                    ProviderMessage.from_dict(m) for m in context["messages"]
                ),
                tools=tool_defs,
                model=settings.model,
                system=str(blueprint.field("system_prompt", "")),
                max_tokens=settings.max_tokens,
                timeout_s=settings.timeout_s,
            )
            # ProviderError (after at most one fallback hop) fails the run loudly
            result, used_provider, pricing = self._complete_with_fallback(
                provider, request, settings, run_id, context,
                remaining_budget=max_model - context["model_calls"],
            )
            payload: dict[str, Any] = {
                "provider": used_provider.id,
                "finish_reason": result.finish_reason,
                "usage": {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
            }
            in_price, out_price = pricing
            if in_price is not None or out_price is not None:
                cost = float(context.get("cost_usd", 0.0))
                cost += result.usage.input_tokens / 1e6 * (in_price or 0.0)
                cost += result.usage.output_tokens / 1e6 * (out_price or 0.0)
                context["cost_usd"] = round(cost, 6)
                payload["cost_usd"] = context["cost_usd"]
            self.db.update_context(
                run_id, context, event_type="agent.model_called", payload=payload
            )
            if (
                settings.cost_budget_usd is not None
                and float(context.get("cost_usd", 0.0)) > settings.cost_budget_usd
            ):
                raise RuntimeError(
                    "Budget exhausted: cost "
                    f"${context['cost_usd']:.6f} exceeds ${settings.cost_budget_usd:.6f}"
                )

            if result.finish_reason == "max_tokens":
                raise RuntimeError(
                    f"Response truncated at max_tokens={settings.max_tokens}; "
                    "raise max_tokens on the profile or agent"
                )
            if not result.tool_calls:
                if not result.text.strip():
                    raise RuntimeError("Provider returned an empty final response")
                self.db.transition(
                    run_id,
                    "COMPLETED",
                    context=context,
                    output={"message": result.text},
                    event_type="run.completed",
                )
                return

            context["messages"].append(
                ProviderMessage(
                    "assistant", result.text, tool_calls=result.tool_calls
                ).to_dict()
            )
            asks = [c for c in result.tool_calls if c.name == "user.ask"]
            regular = [c for c in result.tool_calls if c.name != "user.ask"]

            for call in regular:
                if context["tool_call_seq"] >= max_tools:
                    raise RuntimeError(f"Budget exhausted: {max_tools} tool calls")
                context["tool_call_seq"] += 1
                key = f"{run_id}:call:{context['tool_call_seq']}"
                try:
                    data = self.tools.execute(
                        run_id=run_id,
                        tool_id=call.name,
                        parameters=call.arguments,
                        idempotency_key=key,
                        allowed_tools=blueprint.tools,
                    )
                    content = dumps(data)
                except (ToolPermissionError, ToolInputError, KeyError) as exc:
                    # The denial/validation event is already durable; the model
                    # sees the error as a tool result and may adjust.
                    content = dumps({"error": str(exc)})
                context["messages"].append(
                    ProviderMessage("tool", content, tool_call_id=call.id).to_dict()
                )

            if asks:
                # one durable question per turn; extra asks get a synthetic
                # result so the provider transcript stays well-formed
                for extra in asks[1:]:
                    context["messages"].append(
                        ProviderMessage(
                            "tool",
                            dumps({"error": "only one question per turn; ask again"}),
                            tool_call_id=extra.id,
                        ).to_dict()
                    )
                ask = asks[0]
                question = (
                    str(ask.arguments.get("question", "")).strip()
                    or "Please provide more information."
                )
                self.db.transition_with_interaction(
                    run_id,
                    "WAITING_FOR_USER",
                    kind="user_input",
                    prompt=question,
                    payload={"resume": "llm", "tool_call_id": ask.id},
                    context=context,
                    expected_state="RUNNING",
                )
                return

            self.db.update_context(run_id, context, event_type="agent.tools_applied")

    def _answer_llm(
        self, run: dict[str, Any], interaction: dict[str, Any], answer: Any
    ) -> None:
        text = str(answer).strip()
        if not text:
            raise ValueError("Answer cannot be empty")  # interaction stays PENDING
        try:
            blueprint = self._effective_blueprint(run)
        except RuntimeError as exc:
            # The definition drifted under a paused run: fail the run loudly
            # (it can never resume faithfully) and tell the caller why. The
            # interaction is left untouched as evidence.
            self.db.transition(
                run["id"],
                "FAILED",
                output={"error": str(exc)},
                event_type="run.failed",
                payload={"error": str(exc), "reason": "definition_drift"},
                expected_state=run["state"],
            )
            raise ValueError(str(exc)) from exc
        context = run["context"]
        context["messages"].append(
            ProviderMessage(
                "tool",
                text,
                tool_call_id=str(interaction["payload"].get("tool_call_id", "")),
            ).to_dict()
        )
        context["continuation"] = {"kind": "llm"}
        # Free-text answers may contain anything, so the event stream gets a
        # redacted record; the interaction row keeps the value (M1.2 §8).
        self.db.resolve_interaction_and_transition(
            interaction["id"],
            answer,
            run_id=run["id"],
            expected_kind="user_input",
            new_state="RUNNING",
            context=context,
            event_type="run.resumed",
            payload={"source": "user_input"},
            expected_state="WAITING_FOR_USER",
            redact_answer=True,
        )
        context.pop("continuation", None)  # persists with the next durable write
        try:
            self._llm_loop(run["id"], blueprint, context)
        except Exception as exc:
            self.db.transition(
                run["id"],
                "FAILED",
                output={"error": str(exc)},
                event_type="run.failed",
                payload={"error": str(exc)},
            )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _proposed_action(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "vehicle_id": context["vehicle_id"],
            "event_type": context["event_type"],
            "odometer_km": context["odometer_km"],
            # Pinned when the approval is prepared (M1.5 §1): the user
            # approves the EXACT record — including its date — and a delayed
            # approval or a restart across midnight cannot change it.
            "occurred_at": context["occurred_at"],
        }

    @staticmethod
    def _approval_prompt(context: dict[str, Any]) -> str:
        return (
            f"Log {context['event_type'].replace('_', ' ')} for the 2016 Genesis "
            f"at {context['odometer_km']:,.0f} km, dated {context['occurred_at']}?"
        )

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()

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
    def _match_event_types(text: str) -> tuple[str, ...] | None:
        """Concrete event types a question refers to, else None (M1.5 §2).

        "oil change" is a CATEGORY: an oil-and-filter service is also an
        oil change, so the plain-oil question spans both types, while an
        explicit "oil and filter" question stays exact."""
        if "tire" in text or "tyre" in text:
            return ("tire_replacement",)
        if "insurance" in text:
            return ("insurance_renewal",)
        if "filter" in text and "oil" in text:
            return ("oil_and_filter_change",)
        if "oil" in text:
            return ("oil_change", "oil_and_filter_change")
        if "service" in text:
            return ("maintenance_service",)
        return None

    @staticmethod
    def _detect_event_type(text: str) -> str:
        matched = Runtime._match_event_types(text)
        return matched[0] if matched else "maintenance_service"
