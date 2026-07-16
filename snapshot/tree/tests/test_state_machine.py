"""State-machine and storage-hygiene tests for atlas.db (findings F-03/F-06/F-07)."""
from pathlib import Path

from atlas.db import (
    Database,
    IllegalTransitionError,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
)


def make_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "atlas.db"))


def make_run(db: Database) -> str:
    return db.create_run(
        target_type="agent",
        target_id="car-maintenance-agent",
        target_version=1,
        input_data={"message": "x"},
    )


def test_every_legal_transition_is_accepted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    for source, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            run_id = make_run(db)  # starts CREATED
            # walk the run to `source` first
            if source != "CREATED":
                db.transition(run_id, "RUNNING")
                if source != "RUNNING":
                    db.transition(run_id, source)
            db.transition(run_id, target)
            assert db.get_run(run_id)["state"] == target


def test_terminal_states_cannot_resume(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    for terminal in sorted(TERMINAL_STATES):
        run_id = make_run(db)
        db.transition(run_id, "RUNNING")
        db.transition(run_id, terminal)
        for target in ("RUNNING", "WAITING_FOR_USER", "COMPLETED", "CREATED"):
            try:
                db.transition(run_id, target)
            except IllegalTransitionError:
                pass
            else:
                raise AssertionError(f"{terminal} -> {target} was accepted")
        assert db.get_run(run_id)["state"] == terminal


def test_illegal_and_unknown_transitions_rejected(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    run_id = make_run(db)  # CREATED
    for bad in ("WAITING_FOR_USER", "COMPLETED", "NOT_A_STATE"):
        try:
            db.transition(run_id, bad)
        except IllegalTransitionError:
            pass
        else:
            raise AssertionError(f"CREATED -> {bad} was accepted")
    assert db.get_run(run_id)["state"] == "CREATED"
    # rejected transitions must not consume event sequence numbers
    assert [e["type"] for e in db.get_events(run_id)] == ["run.created"]


def test_expected_state_guard_detects_races(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    run_id = make_run(db)
    db.transition(run_id, "RUNNING", expected_state="CREATED")
    try:
        db.transition(run_id, "COMPLETED", expected_state="CREATED")
    except IllegalTransitionError as exc:
        assert "expected" in str(exc)
    else:
        raise AssertionError("stale expected_state was accepted")


def test_transition_with_interaction_is_atomic(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    run_id = make_run(db)
    db.transition(run_id, "RUNNING")
    interaction_id = db.transition_with_interaction(
        run_id,
        "WAITING_FOR_USER",
        kind="user_input",
        prompt="Mileage?",
        payload={"expected": "number"},
    )
    run = db.get_run(run_id)
    pending = db.pending_interaction(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    assert pending is not None and pending["id"] == interaction_id
    types = [e["type"] for e in db.get_events(run_id)]
    assert types[-2:] == ["run.waiting_for_user", "user_input.requested"]
    # illegal source state must roll everything back
    done = make_run(db)
    db.transition(done, "RUNNING")
    db.transition(done, "COMPLETED")
    try:
        db.transition_with_interaction(done, "WAITING_FOR_USER", kind="user_input", prompt="?")
    except IllegalTransitionError:
        pass
    else:
        raise AssertionError("waiting transition from COMPLETED was accepted")
    assert db.pending_interaction(done) is None
    assert db.get_run(done)["state"] == "COMPLETED"


def test_resolve_interaction_enforces_run_binding_and_kind(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    a, b = make_run(db), make_run(db)
    db.transition(a, "RUNNING")
    db.transition(b, "RUNNING")
    ia = db.transition_with_interaction(a, "WAITING_FOR_USER", kind="user_input", prompt="?")
    db.transition_with_interaction(b, "WAITING_FOR_USER", kind="user_input", prompt="?")
    try:
        db.resolve_interaction(ia, "1", expected_run_id=b)
    except ValueError as exc:
        assert "belong" in str(exc)
    else:
        raise AssertionError("cross-run resolution was accepted")
    try:
        db.resolve_interaction(ia, "1", expected_run_id=a, expected_kind="approval")
    except ValueError as exc:
        assert "kind" in str(exc)
    else:
        raise AssertionError("kind mismatch was accepted")
    # correct binding still works, and the interaction is single-use
    db.resolve_interaction(ia, "1", expected_run_id=a, expected_kind="user_input")
    try:
        db.resolve_interaction(ia, "2", expected_run_id=a)
    except ValueError as exc:
        assert "already resolved" in str(exc)
    else:
        raise AssertionError("double resolution was accepted")


def test_read_paths_do_not_leak_connections(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    run_id = make_run(db)
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():  # non-Linux fallback: exercise reads, assert no error
        for _ in range(200):
            db.get_run(run_id)
        return
    before = len(list(fd_dir.iterdir()))
    for _ in range(200):
        db.get_run(run_id)
        db.get_events(run_id)
        db.list_runs()
        db.pending_interaction(run_id)
    after = len(list(fd_dir.iterdir()))
    assert after - before <= 3, f"leaked {after - before} fds across 800 reads"
