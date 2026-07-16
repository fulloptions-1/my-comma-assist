"""Approved writes survive crashes at every point (M1.1 §5)."""
import hashlib
import uuid
from pathlib import Path

from atlas.db import Database, utc_now
from atlas.runtime import Runtime
from atlas.tools import canonical_dumps

ROOT = Path(__file__).resolve().parents[1]


def to_approval(tmp_path: Path) -> tuple[Runtime, str, dict, dict]:
    rt = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    run_id = rt.create_run("car-maintenance-agent", "log an oil change please")
    mileage = rt.db.pending_interaction(run_id)
    rt.answer(run_id, mileage["id"], "142500")
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_APPROVAL"
    approval = rt.db.pending_interaction(run_id)
    return rt, run_id, run, approval


def maintenance_rows(db: Database) -> list:
    return db.read_maintenance("genesis-2016")


def test_crash_before_side_effect_resumes_and_writes_once(tmp_path: Path) -> None:
    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    # Commit the decision atomically, then "crash" before the tool runs.
    assert rt._commit_car_approval(run, approval, True, proposed) is True
    mid = rt.db.get_run(run_id)
    assert mid["state"] == "RUNNING"
    assert mid["context"]["executing_approval"]["proposed_action"] == proposed
    assert maintenance_rows(rt.db) == []

    restarted = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert len(maintenance_rows(restarted.db)) == 1  # exactly once
    types = [e["type"] for e in restarted.db.get_events(run_id)]
    assert "run.approval_resumed" in types


def test_crash_after_side_effect_recovers_original_result(tmp_path: Path) -> None:
    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    assert rt._commit_car_approval(run, approval, True, proposed) is True
    # The side effect happens, then the process dies before completion.
    rt.tools.execute(
        run_id=run_id,
        tool_id="car.log_maintenance",
        parameters=proposed,
        idempotency_key=f"{run_id}:log-maintenance",
        allowed_tools=("car.log_maintenance",),
    )
    assert len(maintenance_rows(rt.db)) == 1

    restarted = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert len(maintenance_rows(restarted.db)) == 1  # replayed, not repeated
    types = [e["type"] for e in restarted.db.get_events(run_id)]
    assert "tool.replayed" in types and "run.approval_resumed" in types


def _inject_running_execution(rt: Runtime, run_id: str, proposed: dict, *, created_at: str) -> None:
    validated = rt.tools._validate_input(rt.tools.tools["car.log_maintenance"], proposed)
    input_hash = hashlib.sha256(canonical_dumps(validated).encode("utf-8")).hexdigest()
    with rt.db.transaction() as conn:
        conn.execute(
            """INSERT INTO tool_executions
            (id, run_id, tool_id, idempotency_key, input_hash, state, created_at, owner)
            VALUES (?, ?, 'car.log_maintenance', ?, ?, 'RUNNING', ?, 'proc_dead')""",
            (
                f"tool_{uuid.uuid4().hex}",
                run_id,
                f"{run_id}:log-maintenance",
                input_hash,
                created_at,
            ),
        )


def test_immediate_restart_during_side_effect_recovers(tmp_path: Path) -> None:
    """The audit's reproduction: the RUNNING row is FRESH (created_at=now),
    owned by the process that just died. Recovery must retry, not fail."""
    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    assert rt._commit_car_approval(run, approval, True, proposed) is True
    _inject_running_execution(rt, run_id, proposed, created_at=utc_now())

    restarted = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED", finished["output"]
    assert len(maintenance_rows(restarted.db)) == 1
    types = [e["type"] for e in restarted.db.get_events(run_id)]
    assert "run.approval_resumed" in types
    assert any(
        e["type"] == "tool.abandoned" and e["payload"].get("reason") == "process_restart"
        for e in restarted.db.get_events(run_id)
    )


def test_crash_during_side_effect_expired_lease_also_recovers(tmp_path: Path) -> None:
    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    assert rt._commit_car_approval(run, approval, True, proposed) is True
    _inject_running_execution(rt, run_id, proposed, created_at="2020-01-01T00:00:00+00:00")
    restarted = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert len(maintenance_rows(restarted.db)) == 1


def test_repeated_recovery_is_idempotent(tmp_path: Path) -> None:
    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    assert rt._commit_car_approval(run, approval, True, proposed) is True
    _inject_running_execution(rt, run_id, proposed, created_at=utc_now())
    restarted = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")
    for _ in range(3):
        restarted.recover()
    assert restarted.db.get_run(run_id)["state"] == "COMPLETED"
    assert len(maintenance_rows(restarted.db)) == 1
    resumed = [e for e in restarted.db.get_events(run_id) if e["type"] == "run.approval_resumed"]
    assert len(resumed) == 1  # terminal run never re-resumed


def test_live_caller_claims_are_not_stolen(tmp_path: Path) -> None:
    """Two genuinely live gateways: a fresh claim owned by A must block B at
    claim time (only the BOOT-time sweep reclassifies foreign claims)."""
    from atlas.tools import ToolGateway

    rt, run_id, run, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    gw_a = rt.tools
    spec = gw_a.tools["car.log_maintenance"]
    validated = gw_a._validate_input(spec, proposed)
    input_hash = hashlib.sha256(canonical_dumps(validated).encode("utf-8")).hexdigest()
    assert gw_a._claim_execution(run_id, spec, "shared-live", input_hash) is None

    gw_b = ToolGateway(rt.db)  # second live worker, NO boot reclaim
    try:
        gw_b._claim_execution(run_id, spec, "shared-live", input_hash)
    except RuntimeError as exc:
        assert "already in progress" in str(exc)
    else:
        raise AssertionError("live claim was stolen by a second caller")


def test_normal_approval_path_and_reject_are_unchanged(tmp_path: Path) -> None:
    rt, run_id, _, approval = to_approval(tmp_path)
    rt.answer(run_id, approval["id"], True)
    assert rt.db.get_run(run_id)["state"] == "COMPLETED"
    assert len(maintenance_rows(rt.db)) == 1

    rt2, run2, _, approval2 = to_approval(tmp_path.joinpath("b").resolve())
    rt2.answer(run2, approval2["id"], False)
    rejected = rt2.db.get_run(run2)
    assert rejected["state"] == "CANCELLED"
    assert maintenance_rows(rt2.db) == []
