"""Interaction correctness and restart recovery (findings F-01/F-02/F-09)."""
from pathlib import Path

from atlas.db import Database
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def make_runtime(tmp_path: Path) -> Runtime:
    return Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")


def test_invalid_answer_keeps_interaction_pending(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    run_id = rt.create_run("car-maintenance-agent", "Log an oil change")
    q = rt.db.pending_interaction(run_id)
    try:
        rt.answer(run_id, q["id"], "banana")
    except ValueError as exc:
        assert "kilometres" in str(exc)
    else:
        raise AssertionError("invalid mileage accepted")
    # F-01 fix: still waiting, interaction still PENDING, retry succeeds
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_USER"
    still = rt.db.pending_interaction(run_id)
    assert still is not None and still["id"] == q["id"]
    rt.answer(run_id, q["id"], "142300")
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_APPROVAL"


def test_cross_run_answer_rejected_and_both_runs_intact(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    a = rt.create_run("car-maintenance-agent", "Log an oil change")
    b = rt.create_run("car-maintenance-agent", "Log tire replacement")
    qb = rt.db.pending_interaction(b)
    try:
        rt.answer(a, qb["id"], "155000")
    except ValueError as exc:
        assert "belong" in str(exc)
    else:
        raise AssertionError("cross-run answer accepted")
    # F-02 fix: nothing consumed, nothing advanced
    assert rt.db.get_run(a)["state"] == "WAITING_FOR_USER"
    assert rt.db.get_run(b)["state"] == "WAITING_FOR_USER"
    assert rt.db.pending_interaction(a) is not None
    assert rt.db.pending_interaction(b)["id"] == qb["id"]
    # each run still answerable through its own interaction
    rt.answer(b, qb["id"], "155000")
    assert rt.db.get_run(b)["state"] == "WAITING_FOR_APPROVAL"


def test_kind_mismatch_is_rejected(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    a = rt.create_run("car-maintenance-agent", "Log an oil change")      # waits user_input
    b = rt.create_run("car-maintenance-agent", "Log oil at 150000 km")  # waits approval
    approval_b = rt.db.pending_interaction(b)
    # move approval interaction id onto run a's answer call: run binding fires first
    try:
        rt.answer(a, approval_b["id"], True)
    except ValueError:
        pass
    else:
        raise AssertionError("foreign approval accepted")
    assert rt.db.get_run(a)["state"] == "WAITING_FOR_USER"
    assert rt.db.get_run(b)["state"] == "WAITING_FOR_APPROVAL"


def test_recover_fails_orphaned_running_and_created_runs(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    # forge crash leftovers directly in the store
    running = rt.db.create_run(
        target_type="agent", target_id="car-maintenance-agent",
        target_version=1, input_data={"message": "x"},
    )
    rt.db.transition(running, "RUNNING")
    created = rt.db.create_run(
        target_type="agent", target_id="car-maintenance-agent",
        target_version=1, input_data={"message": "y"},
    )
    restarted = make_runtime(tmp_path)
    stats_runs = {r["id"]: r for r in restarted.db.list_runs()}
    assert stats_runs[running]["state"] == "FAILED"
    assert stats_runs[created]["state"] == "FAILED"
    types = [e["type"] for e in restarted.db.get_events(running)]
    assert "run.recovered_orphan" in types


def test_recover_repairs_wedged_waiting_runs_end_to_end(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    # wedge class 1: WAITING_FOR_USER with its interaction destroyed
    w_user = rt.create_run("car-maintenance-agent", "Log an oil change")
    # wedge class 2: WAITING_FOR_APPROVAL with its interaction destroyed
    w_appr = rt.create_run("car-maintenance-agent", "Log oil at 150000 km")
    with rt.db.transaction() as conn:
        conn.execute("DELETE FROM interactions WHERE run_id IN (?, ?)", (w_user, w_appr))
    assert rt.db.pending_interaction(w_user) is None
    assert rt.db.pending_interaction(w_appr) is None

    restarted = make_runtime(tmp_path)
    # both repaired in place, still waiting, answerable to completion
    q = restarted.db.pending_interaction(w_user)
    assert q is not None and q["kind"] == "user_input"
    a = restarted.db.pending_interaction(w_appr)
    assert a is not None and a["kind"] == "approval"
    assert any(e["type"] == "run.repaired" for e in restarted.db.get_events(w_user))

    restarted.answer(w_appr, a["id"], True)
    done = restarted.db.get_run(w_appr)
    assert done["state"] == "COMPLETED"
    assert done["output"]["maintenance_event"]["odometer_km"] == 150000


def test_recover_fails_unrepairable_waiting_run(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    run_id = rt.create_run("car-maintenance-agent", "Log an oil change")
    with rt.db.transaction() as conn:
        conn.execute("DELETE FROM interactions WHERE run_id = ?", (run_id,))
        conn.execute("UPDATE runs SET context_json = '{}' WHERE id = ?", (run_id,))
    restarted = make_runtime(tmp_path)
    run = restarted.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert any(e["type"] == "run.repair_failed" for e in restarted.db.get_events(run_id))


def test_recover_is_idempotent_and_leaves_healthy_runs_alone(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    healthy = rt.create_run("car-maintenance-agent", "Log an oil change")
    for _ in range(3):
        stats = rt.recover()
        assert stats == {
            "orphaned_failed": 0,
            "waiting_repaired": 0,
            "waiting_unrepairable": 0,
            "continuations_resumed": 0,
            "executions_reclaimed": 0,
        }
    assert rt.db.get_run(healthy)["state"] == "WAITING_FOR_USER"
    assert rt.db.pending_interaction(healthy) is not None


def test_tampered_approval_payload_is_refused_and_stays_pending(tmp_path: Path) -> None:
    """Matrix F6: a payload changed after approval was requested must not execute."""
    rt = make_runtime(tmp_path)
    run_id = rt.create_run("car-maintenance-agent", "Log oil at 150000 km")
    approval = rt.db.pending_interaction(run_id)
    # tamper with the stored proposed action after the hash was bound
    tampered = dict(approval["payload"])
    tampered["proposed_action"] = dict(tampered["proposed_action"], odometer_km=1.0)
    with rt.db.transaction() as conn:
        from atlas.db import dumps
        conn.execute(
            "UPDATE interactions SET payload_json = ? WHERE id = ?",
            (dumps(tampered), approval["id"]),
        )
    try:
        rt.answer(run_id, approval["id"], True)
    except ValueError as exc:
        assert "artifact changed" in str(exc)
    else:
        raise AssertionError("tampered approval executed")
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_APPROVAL"
    assert rt.db.pending_interaction(run_id) is not None  # evidence preserved
    assert rt.db.read_maintenance("genesis-2016") == []   # side effect blocked
