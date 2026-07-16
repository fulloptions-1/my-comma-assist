"""The approved payload binds the exact event date (M1.5 §1)."""
from pathlib import Path

from atlas.db import Database
from atlas.runtime import Runtime
from atlas import tools as tools_module

ROOT = Path(__file__).resolve().parents[1]


def to_approval(tmp_path: Path):
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages")
    run_id = rt.create_run("car-maintenance-agent", "log an oil change please")
    rt.answer(run_id, rt.db.pending_interaction(run_id)["id"], "142500")
    approval = rt.db.pending_interaction(run_id)
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_APPROVAL"
    return rt, run_id, approval


def test_proposed_action_pins_occurred_at_and_hash_binds_it(tmp_path: Path) -> None:
    rt, run_id, approval = to_approval(tmp_path)
    proposed = approval["payload"]["proposed_action"]
    assert "occurred_at" in proposed and len(proposed["occurred_at"]) == 10  # ISO date
    assert proposed["occurred_at"] in approval["prompt"]  # shown to the user

    tampered = {**proposed, "occurred_at": "1999-12-31"}
    assert rt._hash_payload(proposed) != rt._hash_payload(tampered)
    assert approval["artifact_hash"] == rt._hash_payload(proposed)


def test_restart_across_date_boundary_keeps_pinned_date(tmp_path: Path) -> None:
    rt, run_id, approval = to_approval(tmp_path)
    pinned_date = approval["payload"]["proposed_action"]["occurred_at"]

    class FrozenFutureDate:
        @staticmethod
        def now(tz=None):
            import datetime as real

            return real.datetime(2031, 1, 1, tzinfo=tz)

    saved = tools_module.datetime
    tools_module.datetime = FrozenFutureDate  # "midnight passed" for the handler
    try:
        restarted = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages")
        restarted.answer(run_id, approval["id"], True)
    finally:
        tools_module.datetime = saved

    rows = restarted.db.read_maintenance("genesis-2016")
    assert len(rows) == 1
    assert rows[0]["occurred_at"] == pinned_date          # never regenerated
    assert "2031" not in rows[0]["occurred_at"]


def test_replay_returns_exactly_the_same_record(tmp_path: Path) -> None:
    rt, run_id, approval = to_approval(tmp_path)
    rt.answer(run_id, approval["id"], True)
    completed = rt.db.get_run(run_id)
    pinned = completed["context"]["executing_approval"]["proposed_action"]
    first = rt.db.read_maintenance("genesis-2016")[0]

    replayed = rt.tools.execute(
        run_id=run_id,
        tool_id="car.log_maintenance",
        parameters=pinned,
        idempotency_key=f"{run_id}:log-maintenance",
    )
    assert replayed["id"] == first["id"]
    assert replayed["occurred_at"] == first["occurred_at"] == pinned["occurred_at"]
    assert len(rt.db.read_maintenance("genesis-2016")) == 1


def test_rejected_approval_writes_nothing(tmp_path: Path) -> None:
    rt, run_id, approval = to_approval(tmp_path)
    rt.answer(run_id, approval["id"], False)
    assert rt.db.get_run(run_id)["state"] == "CANCELLED"
    assert rt.db.read_maintenance("genesis-2016") == []
