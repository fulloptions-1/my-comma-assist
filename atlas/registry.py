from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PackageValidationError(ValueError):
    pass


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


class PackageRegistry:
    def __init__(self, root: Path, known_tools: set[str]) -> None:
        self.root = root
        self.known_tools = known_tools
        self.agents: dict[str, AgentBlueprint] = {}

    def load(self) -> None:
        errors: list[str] = []
        agents: dict[str, AgentBlueprint] = {}

        for manifest_path in sorted(self.root.glob("*/manifest.yaml")):
            package_dir = manifest_path.parent
            manifest = self._read_yaml(manifest_path)
            package_id = str(manifest.get("id", "")).strip()
            if not package_id:
                errors.append(f"{manifest_path}: missing package id")
                continue

            for relative in manifest.get("agents", []):
                path = package_dir / str(relative)
                raw = self._read_yaml(path)
                agent_id = str(raw.get("id", "")).strip()
                if not agent_id:
                    errors.append(f"{path}: missing agent id")
                    continue
                if agent_id in agents:
                    errors.append(f"duplicate agent id: {agent_id}")
                    continue

                tools = tuple(str(item) for item in raw.get("tools", []))
                unknown = sorted(set(tools) - self.known_tools)
                if unknown:
                    errors.append(f"{path}: unknown tools: {', '.join(unknown)}")

                handler = str(raw.get("handler", "")).strip()
                if not handler:
                    errors.append(f"{path}: missing handler")

                agents[agent_id] = AgentBlueprint(
                    id=agent_id,
                    version=int(raw.get("version", 1)),
                    name=str(raw.get("name", agent_id)),
                    description=str(raw.get("description", "")),
                    handler=handler,
                    tools=tools,
                    intents=tuple(str(item).lower() for item in raw.get("intents", [])),
                    package=package_id,
                )

        if errors:
            raise PackageValidationError("\n".join(errors))
        self.agents = agents

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

    def resolve(self, query: str) -> AgentBlueprint:
        normalized = query.lower()
        scored: list[tuple[int, AgentBlueprint]] = []
        for agent in self.agents.values():
            score = sum(1 for intent in agent.intents if intent in normalized)
            if score:
                scored.append((score, agent))
        if not scored:
            return self.get_agent("concierge-agent")
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return scored[0][1]
