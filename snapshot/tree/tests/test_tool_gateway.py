"""Tool Gateway policy tests (findings F-04/F-05; matrix E1-E9)."""
import threading
import time
from pathlib import Path

from atlas.db import Database
from atlas.tools import (
    FieldSpec,
    ToolExecutionTimeout,
    ToolGateway,
    ToolInputError,
    ToolPermissionError,
    ToolSpec,
)


def make_gateway(tmp_path: Path) -> ToolGateway:
    return ToolGateway(Database(str(tmp_path / "atlas.db")))


def make_run(gw: ToolGateway) -> str:
    return gw.db.create_run(
        target_type="agent", target_id="t", target_version=1, input_data={}
    )


def test_permission_denied_for_undeclared_tool(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    try:
        gw.execute(
            run_id=run_id,
            tool_id="car.log_maintenance",
            parameters={"event_type": "oil_change", "odometer_km": 1000},
            idempotency_key="k1",
            allowed_tools=("car.read_history",),
        )
    except ToolPermissionError:
        pass
    else:
        raise AssertionError("undeclared side-effect tool executed")
    assert gw.db.read_maintenance("genesis-2016") == []
    assert any(e["type"] == "tool.denied" for e in gw.db.get_events(run_id))


def test_input_schema_rejects_bad_types_and_unknown_fields(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    for params, needle in [
        ({"event_type": "oil_change"}, "missing required field: odometer_km"),
        ({"event_type": "oil_change", "odometer_km": "many"}, "expected float"),
        ({"event_type": "oil_change", "odometer_km": 1, "bogus": 1}, "unknown fields"),
    ]:
        try:
            gw.execute(
                run_id=run_id,
                tool_id="car.log_maintenance",
                parameters=params,
                idempotency_key="schema-key",
                allowed_tools=("car.log_maintenance",),
            )
        except ToolInputError as exc:
            assert needle in str(exc)
        else:
            raise AssertionError(f"accepted invalid input: {params}")
    # rejected inputs must not burn the idempotency key
    result = gw.execute(
        run_id=run_id,
        tool_id="car.log_maintenance",
        parameters={"event_type": "oil_change", "odometer_km": 1234},
        idempotency_key="schema-key",
        allowed_tools=("car.log_maintenance",),
    )
    assert result["odometer_km"] == 1234


def test_stale_running_execution_can_retry_and_side_effect_stays_single(
    tmp_path: Path,
) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    params = {"event_type": "oil_change", "odometer_km": 5000.0}
    from atlas.tools import canonical_dumps

    cleaned = {"vehicle_id": "genesis-2016", "event_type": "oil_change", "odometer_km": 5000.0}
    input_hash = __import__("hashlib").sha256(canonical_dumps(cleaned).encode()).hexdigest()
    # simulate a crash: RUNNING row from long ago, but the domain side effect
    # already happened (worst case for duplication)
    with gw.db.transaction() as conn:
        conn.execute(
            """INSERT INTO tool_executions
               (id, run_id, tool_id, idempotency_key, input_hash, state, created_at)
               VALUES ('tool_dead', ?, 'car.log_maintenance', 'crash-key', ?, 'RUNNING',
                       '2020-01-01T00:00:00+00:00')""",
            (run_id, input_hash),
        )
    gw._log_maintenance(params, "crash-key")  # side effect landed pre-crash
    assert len(gw.db.read_maintenance("genesis-2016")) == 1

    result = gw.execute(
        run_id=run_id,
        tool_id="car.log_maintenance",
        parameters=params,
        idempotency_key="crash-key",
        allowed_tools=("car.log_maintenance",),
    )
    assert result["odometer_km"] == 5000
    assert len(gw.db.read_maintenance("genesis-2016")) == 1  # still exactly one
    types = [e["type"] for e in gw.db.get_events(run_id)]
    assert "tool.abandoned" in types and "tool.retried" in types


def test_fresh_running_execution_still_blocks(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    import hashlib

    from atlas.db import utc_now
    from atlas.tools import canonical_dumps

    # Hash the VALIDATED input (defaults applied) exactly as execute() does;
    # hardcoding the raw dict broke when the schema gained event_type (M1.4 §4).
    validated = gw._validate_input(gw.tools["car.read_history"], {"vehicle_id": "genesis-2016"})
    h = hashlib.sha256(canonical_dumps(validated).encode()).hexdigest()
    with gw.db.transaction() as conn:
        conn.execute(
            """INSERT INTO tool_executions
               (id, run_id, tool_id, idempotency_key, input_hash, state, created_at)
               VALUES ('tool_live', ?, 'car.read_history', 'live-key', ?, 'RUNNING', ?)""",
            (run_id, h, utc_now()),
        )
    try:
        gw.execute(
            run_id=run_id,
            tool_id="car.read_history",
            parameters={"vehicle_id": "genesis-2016"},
            idempotency_key="live-key",
            allowed_tools=("car.read_history",),
        )
    except RuntimeError as exc:
        assert "in progress" in str(exc)
    else:
        raise AssertionError("fresh RUNNING execution did not block")


def test_same_key_different_input_rejected(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    common = dict(run_id=run_id, tool_id="car.log_maintenance", idempotency_key="dup")
    gw.execute(
        parameters={"event_type": "oil_change", "odometer_km": 100.0},
        allowed_tools=("car.log_maintenance",),
        **common,
    )
    try:
        gw.execute(
            parameters={"event_type": "oil_change", "odometer_km": 999.0},
            allowed_tools=("car.log_maintenance",),
            **common,
        )
    except ValueError as exc:
        assert "different input" in str(exc)
    else:
        raise AssertionError("conflicting reuse accepted")


def test_resource_lock_serializes_conflicting_writes(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    order: list[str] = []
    barrier = threading.Barrier(2)

    slow_done = threading.Event()

    def slow(params, key):
        order.append("slow-start")
        time.sleep(0.4)
        order.append("slow-end")
        slow_done.set()
        return {"ok": True}

    def fast(params, key):
        order.append(f"fast-start(slow_done={slow_done.is_set()})")
        return {"ok": True}

    gw.register(ToolSpec(id="t.slow", side_effect=True, handler=slow,
                         schema={"vehicle_id": FieldSpec(str, required=True)},
                         timeout_s=5, lock_key="vehicle:{vehicle_id}"))
    gw.register(ToolSpec(id="t.fast", side_effect=True, handler=fast,
                         schema={"vehicle_id": FieldSpec(str, required=True)},
                         timeout_s=5, lock_key="vehicle:{vehicle_id}"))

    def call(tool_id, key, delay):
        barrier.wait()
        time.sleep(delay)
        gw.execute(run_id=run_id, tool_id=tool_id,
                   parameters={"vehicle_id": "genesis-2016"},
                   idempotency_key=key, allowed_tools=(tool_id,))

    a = threading.Thread(target=call, args=("t.slow", "lk1", 0.0))
    b = threading.Thread(target=call, args=("t.fast", "lk2", 0.1))
    a.start(); b.start(); a.join(); b.join()
    assert order[0] == "slow-start"
    assert "fast-start(slow_done=True)" in order, f"lock did not serialize: {order}"


def test_timeout_marks_failed_and_raises(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    gw.register(ToolSpec(id="t.hang", side_effect=False,
                         handler=lambda p, k: time.sleep(2) or {},
                         schema={}, timeout_s=0.2))
    try:
        gw.execute(run_id=run_id, tool_id="t.hang", parameters={},
                   idempotency_key="hang", allowed_tools=("t.hang",))
    except ToolExecutionTimeout:
        pass
    else:
        raise AssertionError("timeout not raised")
    events = gw.db.get_events(run_id)
    failed = [e for e in events if e["type"] == "tool.failed"]
    assert failed and failed[-1]["payload"]["error_class"] == "ToolExecutionTimeout"


def test_oversized_result_trimmed_from_event_but_full_in_execution(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    big = {"blob": "x" * 10_000}
    gw.register(ToolSpec(id="t.big", side_effect=False,
                         handler=lambda p, k: big, schema={}, timeout_s=5))
    gw.execute(run_id=run_id, tool_id="t.big", parameters={},
               idempotency_key="big", allowed_tools=("t.big",))
    ev = [e for e in gw.db.get_events(run_id) if e["type"] == "tool.completed"][-1]
    assert ev["payload"]["result"]["truncated"] is True
    assert ev["payload"]["result"]["size"] > 10_000
    # replay returns the untrimmed result
    replay = gw.execute(run_id=run_id, tool_id="t.big", parameters={},
                        idempotency_key="big", allowed_tools=("t.big",))
    assert replay == big


def register_probe_tools(gw: ToolGateway, sleep_s: float, timeout_s: float) -> None:
    calls = {"count": 0}
    gw._probe_calls = calls

    def slow_read(params, key):
        time.sleep(sleep_s)
        return {"ok": True}

    def slow_write(params, key):
        calls["count"] += 1
        time.sleep(sleep_s)
        return {"written": calls["count"]}

    gw.tools["probe.read"] = ToolSpec(
        id="probe.read", description="", schema={}, handler=slow_read,
        side_effect=False, timeout_s=timeout_s,
    )
    gw.tools["probe.write"] = ToolSpec(
        id="probe.write", description="", schema={}, handler=slow_write,
        side_effect=True, timeout_s=timeout_s,
    )


def test_read_only_timeout_returns_at_deadline(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    register_probe_tools(gw, sleep_s=3.0, timeout_s=0.2)
    started = time.monotonic()
    try:
        gw.execute(run_id=run_id, tool_id="probe.read", parameters={}, idempotency_key="ro1")
    except ToolExecutionTimeout:
        elapsed = time.monotonic() - started
    else:
        raise AssertionError("read-only timeout did not raise")
    assert elapsed < 1.5, f"caller blocked {elapsed:.2f}s past a 0.2s deadline"


def test_side_effect_overrun_completes_once_and_is_recorded(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    register_probe_tools(gw, sleep_s=0.3, timeout_s=0.1)
    result = gw.execute(
        run_id=run_id, tool_id="probe.write", parameters={}, idempotency_key="se1"
    )
    assert result == {"written": 1}          # completed exactly once, not faked-cancelled
    assert gw._probe_calls["count"] == 1
    done = [e for e in gw.db.get_events(run_id) if e["type"] == "tool.completed"]
    assert done[-1]["payload"]["timeout_exceeded"] is True
    assert done[-1]["payload"]["elapsed_s"] >= 0.1


def test_idempotency_key_bound_to_run_and_tool(tmp_path: Path) -> None:
    gw = make_gateway(tmp_path)
    run_a, run_b = make_run(gw), make_run(gw)
    params = {"vehicle_id": "genesis-2016"}
    first = gw.execute(
        run_id=run_a, tool_id="car.read_history", parameters=params, idempotency_key="shared"
    )
    # replay: same run + tool + input -> original result + tool.replayed event
    again = gw.execute(
        run_id=run_a, tool_id="car.read_history", parameters=params, idempotency_key="shared"
    )
    assert again == first
    assert any(e["type"] == "tool.replayed" for e in gw.db.get_events(run_a))

    try:  # cross-run reuse
        gw.execute(
            run_id=run_b, tool_id="car.read_history", parameters=params, idempotency_key="shared"
        )
    except ValueError as exc:
        assert "different" in str(exc) and "run" in str(exc)
    else:
        raise AssertionError("cross-run idempotency reuse accepted")

    try:  # cross-tool reuse within the same run
        gw.execute(
            run_id=run_a,
            tool_id="car.log_maintenance",
            parameters={"event_type": "oil_change", "odometer_km": 1200},
            idempotency_key="shared",
        )
    except ValueError as exc:
        assert "tool" in str(exc)
    else:
        raise AssertionError("cross-tool idempotency reuse accepted")


def test_non_finite_numbers_are_rejected(tmp_path: Path) -> None:
    """M1.4 §8: NaN/inf tool inputs are validation failures, never rows."""
    gw = make_gateway(tmp_path)
    run_id = make_run(gw)
    for bad in (float("inf"), float("-inf"), float("nan")):
        try:
            gw.execute(
                run_id=run_id,
                tool_id="car.log_maintenance",
                parameters={"event_type": "oil_change", "odometer_km": bad},
                idempotency_key=f"nf-{bad!r}",
                allowed_tools=("car.log_maintenance",),
            )
        except ValueError as exc:
            assert "finite" in str(exc)
        else:
            raise AssertionError(f"accepted odometer_km={bad!r}")
    assert gw.db.read_maintenance("genesis-2016") == []
