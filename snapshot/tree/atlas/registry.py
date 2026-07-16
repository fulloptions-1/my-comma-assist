from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PackageValidationError(ValueError):
    pass


KNOWN_MANIFEST_FIELDS = {"id", "version", "agents"}
KNOWN_AGENT_FIELDS = {
    "id",
    "version",
    "name",
    "description",
    "handler",
    "tools",
    "intents",
}


@dataclass(frozen=True)
class AgentBlueprint:
    id: str
    version: int
    name: str
    description: str
    handler: str
    tools: tuple[str, ...]
    intents: tuple[str, ...]
    package: str
    fingerprint: str = ""
    extra: tuple[tuple[str, Any], ...] = ()

    def field(self, name: str, default: Any = None) -> Any:
        for key, value in self.extra:
            if key == name:
                return value
        return default


class PackageRegistry:
    """Loads and validates package/agent definitions.

    Fails loudly: unknown fields, unknown handlers, duplicate ids, bad types,
    and stale tool references are rejected as a batch of actionable errors —
    nothing is silently filtered or defaulted away.
    """

    def __init__(
        self,
        root: Path,
        known_tools: set[str],
        known_handlers: set[str] | None = None,
        extra_agent_fields: set[str] | None = None,
    ) -> None:
        self.root = root
        self.known_tools = known_tools
        self.known_handlers = known_handlers
        self.extra_agent_fields = extra_agent_fields or set()
        self.agents: dict[str, AgentBlueprint] = {}

    def load(self) -> None:
        errors: list[str] = []
        agents: dict[str, AgentBlueprint] = {}
        package_ids: dict[str, Path] = {}

        for manifest_path in sorted(self.root.glob("*/manifest.yaml")):
            package_dir = manifest_path.parent
            manifest = self._read_yaml(manifest_path)

            unknown_manifest = sorted(set(manifest) - KNOWN_MANIFEST_FIELDS)
            if unknown_manifest:
                errors.append(
                    f"{manifest_path}: unknown fields: {', '.join(unknown_manifest)}"
                )

            package_id = str(manifest.get("id", "")).strip()
            if not package_id:
                errors.append(f"{manifest_path}: missing package id")
                continue
            if package_id in package_ids:
                errors.append(
                    f"duplicate package id {package_id!r} "
                    f"({package_ids[package_id]} and {manifest_path})"
                )
                continue
            package_ids[package_id] = manifest_path

            agent_paths = manifest.get("agents", [])
            if not isinstance(agent_paths, list) or not all(
                isinstance(item, str) for item in agent_paths
            ):
                errors.append(f"{manifest_path}: 'agents' must be a list of paths")
                continue

            for relative in agent_paths:
                path = package_dir / relative
                raw = self._read_yaml(path)
                agent_errors = self._validate_agent(path, raw)
                errors.extend(agent_errors)
                if agent_errors:
                    continue  # never build blueprints from invalid definitions
                agent_id = str(raw.get("id", "")).strip()
                if not agent_id:
                    continue
                if agent_id in agents:
                    errors.append(f"duplicate agent id: {agent_id}")
                    continue
                agents[agent_id] = self._to_blueprint(raw, agent_id, package_id)

        if errors:
            raise PackageValidationError("\n".join(errors))
        self.agents = agents

    def _validate_agent(self, path: Path, raw: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        allowed = KNOWN_AGENT_FIELDS | self.extra_agent_fields
        unknown = sorted(set(raw) - allowed)
        if unknown:
            errors.append(f"{path}: unknown fields: {', '.join(unknown)}")

        if not str(raw.get("id", "")).strip():
            errors.append(f"{path}: missing agent id")

        version = raw.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            errors.append(f"{path}: version must be a positive integer, got {version!r}")

        handler = str(raw.get("handler", "")).strip()
        if not handler:
            errors.append(f"{path}: missing handler")
        elif self.known_handlers is not None and handler not in self.known_handlers:
            errors.append(
                f"{path}: unknown handler {handler!r} "
                f"(known: {', '.join(sorted(self.known_handlers))})"
            )

        for list_field in ("tools", "intents"):
            value = raw.get(list_field, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                errors.append(f"{path}: {list_field!r} must be a list of strings")

        tools = raw.get("tools", [])
        if isinstance(tools, list):
            unknown_tools = sorted(
                {str(item) for item in tools if isinstance(item, str)}
                - self.known_tools
            )
            if unknown_tools:
                errors.append(f"{path}: unknown tools: {', '.join(unknown_tools)}")
        return errors

    def _to_blueprint(
        self, raw: dict[str, Any], agent_id: str, package_id: str
    ) -> AgentBlueprint:
        tools = tuple(str(item) for item in raw.get("tools", []) if isinstance(item, str))
        intents = tuple(
            str(item).lower() for item in raw.get("intents", []) if isinstance(item, str)
        )
        extra = tuple(
            (key, raw[key]) for key in sorted(self.extra_agent_fields) if key in raw
        )
        fingerprint = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        return AgentBlueprint(
            id=agent_id,
            version=int(raw.get("version", 1)),
            name=str(raw.get("name", agent_id)),
            description=str(raw.get("description", "")),
            handler=str(raw.get("handler", "")).strip(),
            tools=tools,
            intents=intents,
            package=package_id,
            fingerprint=fingerprint,
            extra=extra,
        )

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise PackageValidationError(f"missing definition: {path}")
        with path.open("r", encoding="utf-8") as file:
            value = yaml.safe_load(file) or {}
        if not isinstance(value, dict):
            raise PackageValidationError(f"{path}: expected a mapping")
        return value

    def get_agent(self, agent_id: str) -> AgentBlueprint:
        try:
            return self.agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Unknown agent: {agent_id}") from exc

    def match(self, query: str) -> list[tuple[int, AgentBlueprint]]:
        """Intent-scored candidates, best first (concierge never competes)."""
        normalized = query.lower()
        scored: list[tuple[int, AgentBlueprint]] = []
        for agent in self.agents.values():
            if agent.handler == "concierge":
                continue
            score = sum(1 for intent in agent.intents if intent in normalized)
            if score:
                scored.append((score, agent))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return scored

    def resolve(self, query: str) -> AgentBlueprint:
        """Single unambiguous best specialist, else the concierge (which
        delegates general chat to the assistant and asks on ties)."""
        scored = self.match(query)
        if not scored:
            return self.get_agent("concierge-agent")
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return self.get_agent("concierge-agent")
        return scored[0][1]
