from pathlib import Path

from atlas.db import Database
from atlas.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]


def make_runtime(tmp_path: Path) -> Runtime:
    return Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")


def test_car_question_approval_and_idempotent_write(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    run_id = runtime.create_run("car-maintenance-agent", "Log an oil change")
    run = runtime.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"

    question = runtime.db.pending_interaction(run_id)
    assert question is not None
    runtime.answer(run_id, question["id"], "142,300")
    assert runtime.db.get_run(run_id)["state"] == "WAITING_FOR_APPROVAL"

    approval = runtime.db.pending_interaction(run_id)
    assert approval is not None
    runtime.answer(run_id, approval["id"], True)
    completed = runtime.db.get_run(run_id)
    assert completed["state"] == "COMPLETED"
    assert completed["output"]["maintenance_event"]["odometer_km"] == 142300

    # Replay with the EXACT pinned action the user approved (which, since
    # M1.5 §1, includes occurred_at) — the idempotency contract binds the
    # full payload, so a hand-built subset would rightly be rejected.
    pinned = completed["context"]["executing_approval"]["proposed_action"]
    result = runtime.tools.execute(
        run_id=run_id,
        tool_id="car.log_maintenance",
        parameters=pinned,
        idempotency_key=f"{run_id}:log-maintenance",
    )
    assert result["odometer_km"] == 142300
    assert len(runtime.db.read_maintenance("genesis-2016")) == 1


def test_history_survives_runtime_restart(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    run_id = runtime.create_run("car-maintenance-agent", "Log oil at 150000 km")
    approval = runtime.db.pending_interaction(run_id)
    assert approval is not None
    runtime.answer(run_id, approval["id"], True)

    restarted = make_runtime(tmp_path)
    history_run = restarted.create_run("auto", "When was my last oil change?")
    result = restarted.db.get_run(history_run)
    assert result["state"] == "COMPLETED"
    assert "150,000 km" in result["output"]["message"]


def test_package_registry_rejects_unknown_tool(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    agent_dir = packages / "bad" / "agents"
    agent_dir.mkdir(parents=True)
    (packages / "bad" / "manifest.yaml").write_text(
        "id: bad\nagents:\n  - agents/bad.yaml\n", encoding="utf-8"
    )
    (agent_dir / "bad.yaml").write_text(
        "id: bad-agent\nversion: 1\nname: Bad\nhandler: concierge\ntools:\n  - missing.tool\n",
        encoding="utf-8",
    )
    from atlas.registry import PackageRegistry, PackageValidationError

    registry = PackageRegistry(packages, {"known.tool"})
    try:
        registry.load()
    except PackageValidationError as exc:
        assert "unknown tools" in str(exc)
    else:
        raise AssertionError("invalid package was accepted")
