"""Registry validation depth (finding F-10; matrix A3/A4/A7)."""
from pathlib import Path

from atlas.db import Database
from atlas.registry import PackageRegistry, PackageValidationError
from atlas.runtime import LLM_AGENT_FIELDS, Runtime

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"car.read_history", "car.log_maintenance"}


def write_pkg(root: Path, pkg: str, manifest: str, agents: dict[str, str]) -> None:
    d = root / pkg
    (d / "agents").mkdir(parents=True)
    (d / "manifest.yaml").write_text(manifest, encoding="utf-8")
    for name, body in agents.items():
        (d / "agents" / name).write_text(body, encoding="utf-8")


def expect_error(root: Path, needle: str, known_handlers: set[str] | None = None) -> None:
    registry = PackageRegistry(root, TOOLS, known_handlers=known_handlers)
    try:
        registry.load()
    except PackageValidationError as exc:
        assert needle in str(exc), f"expected {needle!r} in: {exc}"
    else:
        raise AssertionError(f"load accepted invalid package (wanted {needle!r})")


AGENT_OK = """id: a-1
version: 1
name: A
description: d
handler: concierge
tools: []
intents: [alpha]
"""


def test_duplicate_package_id_rejected(tmp_path: Path) -> None:
    for pkg in ("one", "two"):
        write_pkg(
            tmp_path, pkg,
            "id: same-package\nversion: 1\nagents:\n  - agents/a.yaml\n",
            {"a.yaml": AGENT_OK.replace("a-1", f"a-{pkg}")},
        )
    expect_error(tmp_path, "duplicate package id")


def test_unknown_handler_rejected_when_handlers_known(tmp_path: Path) -> None:
    write_pkg(
        tmp_path, "p",
        "id: p\nversion: 1\nagents:\n  - agents/a.yaml\n",
        {"a.yaml": AGENT_OK.replace("handler: concierge", "handler: warp_drive")},
    )
    expect_error(tmp_path, "unknown handler", known_handlers={"concierge", "car_maintenance"})


def test_bad_version_rejected(tmp_path: Path) -> None:
    for bad in ("0", "-3", "two", "true"):
        root = tmp_path / f"v{bad}"
        root.mkdir()
        write_pkg(
            root, "p",
            "id: p\nversion: 1\nagents:\n  - agents/a.yaml\n",
            {"a.yaml": AGENT_OK.replace("version: 1", f"version: {bad}")},
        )
        expect_error(root, "version must be a positive integer")


def test_unknown_agent_and_manifest_fields_rejected(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_a.mkdir()
    write_pkg(
        root_a, "p",
        "id: p\nversion: 1\nagents:\n  - agents/a.yaml\n",
        {"a.yaml": AGENT_OK + "surprise_field: 1\n"},
    )
    expect_error(root_a, "unknown fields: surprise_field")

    root_m = tmp_path / "m"
    root_m.mkdir()
    write_pkg(
        root_m, "p",
        "id: p\nversion: 1\nmystery: yes\nagents:\n  - agents/a.yaml\n",
        {"a.yaml": AGENT_OK},
    )
    expect_error(root_m, "unknown fields: mystery")


def test_wrong_typed_tool_and_intent_lists_rejected(tmp_path: Path) -> None:
    write_pkg(
        tmp_path, "p",
        "id: p\nversion: 1\nagents:\n  - agents/a.yaml\n",
        {"a.yaml": AGENT_OK.replace("intents: [alpha]", "intents: alpha")},
    )
    expect_error(tmp_path, "'intents' must be a list of strings")


def test_fingerprint_is_stable_and_recorded_on_runs(tmp_path: Path) -> None:
    registry_a = PackageRegistry(ROOT / "packages", TOOLS, extra_agent_fields=LLM_AGENT_FIELDS)
    registry_a.load()
    registry_b = PackageRegistry(ROOT / "packages", TOOLS, extra_agent_fields=LLM_AGENT_FIELDS)
    registry_b.load()
    fp_a = registry_a.get_agent("car-maintenance-agent").fingerprint
    fp_b = registry_b.get_agent("car-maintenance-agent").fingerprint
    assert fp_a and fp_a == fp_b, "fingerprint must be stable across loads"

    rt = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    run_id = rt.create_run("car-maintenance-agent", "Log oil at 150000 km")
    run = rt.db.get_run(run_id)
    assert run["context"]["definition_fingerprint"] == fp_a
