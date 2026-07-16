"""Generic llm agent runtime (matrix B10 partial, D-series kernel side)."""
from pathlib import Path

from atlas.db import Database
from atlas.providers import (
    FakeProvider,
    ProviderError,
    ProviderResult,
    ToolCall,
)
from atlas.registry import PackageValidationError
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def make_runtime(tmp_path: Path, script) -> Runtime:
    return Runtime(
        Database(str(tmp_path / "atlas.db")),
        ROOT / "packages",
        providers={"fake": FakeProvider(script=script)},
    )


def turn_ask(question: str, call_id: str = "ask_1") -> ProviderResult:
    return ProviderResult(
        finish_reason="tool_use",
        tool_calls=(ToolCall(id=call_id, name="user.ask", arguments={"question": question}),),
    )


def turn_tool(name: str, args: dict, call_id: str = "tc_1") -> ProviderResult:
    return ProviderResult(
        finish_reason="tool_use",
        tool_calls=(ToolCall(id=call_id, name=name, arguments=args),),
    )


def turn_text(text: str) -> ProviderResult:
    return ProviderResult(finish_reason="end", text=text)


def test_llm_ask_restart_answer_tool_complete(tmp_path: Path) -> None:
    rt = make_runtime(
        tmp_path,
        script=[turn_ask("Which vehicle?")],
    )
    run_id = rt.create_run("assistant-agent", "What was my last maintenance?")
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    question = rt.db.pending_interaction(run_id)
    assert question["prompt"] == "Which vehicle?"
    assert question["payload"]["resume"] == "llm"

    # process restart: durable wait must survive, message history in context
    restarted = Runtime(
        Database(str(tmp_path / "atlas.db")),
        ROOT / "packages",
        providers={
            "fake": FakeProvider(
                script=[
                    turn_tool("car.read_history", {"vehicle_id": "genesis-2016"}, "tc_7"),
                    turn_text("Your last event is on record."),
                ]
            )
        },
    )
    still = restarted.db.get_run(run_id)
    assert still["state"] == "WAITING_FOR_USER"
    assert restarted.db.pending_interaction(run_id)["id"] == question["id"]

    restarted.answer(run_id, question["id"], "the genesis")
    done = restarted.db.get_run(run_id)
    assert done["state"] == "COMPLETED"
    assert done["output"]["message"] == "Your last event is on record."
    # transcript: user, assistant(ask), tool(answer), assistant(tool call), tool(result)
    roles = [m["role"] for m in done["context"]["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]
    types = [e["type"] for e in restarted.db.get_events(run_id)]
    assert "tool.completed" in types and "run.completed" in types
    assert done["context"]["model_calls"] == 3


def test_llm_empty_answer_keeps_interaction_pending(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path, script=[turn_ask("Which vehicle?")])
    run_id = rt.create_run("assistant-agent", "history please")
    q = rt.db.pending_interaction(run_id)
    try:
        rt.answer(run_id, q["id"], "   ")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty answer accepted")
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_USER"
    assert rt.db.pending_interaction(run_id)["id"] == q["id"]


def test_llm_model_call_budget_fails_loudly(tmp_path: Path) -> None:
    # 9 tool turns exceed the assistant's max_model_calls of 8
    script = [
        turn_tool("car.read_history", {}, f"tc_{i}") for i in range(9)
    ]
    rt = make_runtime(tmp_path, script=script)
    run_id = rt.create_run("assistant-agent", "loop forever")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "Budget exhausted: 8 model calls" in run["output"]["error"]


def test_llm_undeclared_tool_is_denied_but_visible_to_model(tmp_path: Path) -> None:
    # assistant-agent declares only car.read_history; the model tries the
    # side-effecting write, the gateway denies it durably, the model sees the
    # error and finishes.
    rt = make_runtime(
        tmp_path,
        script=[
            turn_tool("car.log_maintenance", {"event_type": "oil_change", "odometer_km": 1.0}),
            turn_text("I cannot write maintenance records."),
        ],
    )
    run_id = rt.create_run("assistant-agent", "log an oil change at 150000")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    assert rt.db.read_maintenance("genesis-2016") == []  # side effect blocked
    types = [e["type"] for e in rt.db.get_events(run_id)]
    assert "tool.denied" in types
    tool_msgs = [m for m in run["context"]["messages"] if m["role"] == "tool"]
    assert "allow-list" in tool_msgs[-1]["content"]


def test_llm_provider_error_fails_run_with_category(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path, script=[ProviderError("rate_limit", "429 slow down")])
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "[rate_limit]" in run["output"]["error"]


def test_llm_agents_cannot_declare_side_effect_tools_yet(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    (packages / "bad" / "agents").mkdir(parents=True)
    (packages / "bad" / "manifest.yaml").write_text(
        "id: bad\nversion: 1\nagents:\n  - agents/w.yaml\n", encoding="utf-8"
    )
    (packages / "bad" / "agents" / "w.yaml").write_text(
        "id: writer\nversion: 1\nname: W\ndescription: d\nhandler: llm\n"
        "provider: fake\ntools:\n  - car.log_maintenance\nintents: [w]\n",
        encoding="utf-8",
    )
    try:
        Runtime(Database(str(tmp_path / "atlas.db")), packages)
    except PackageValidationError as exc:
        assert "side-effect tools" in str(exc)
    else:
        raise AssertionError("llm agent with side-effect tool loaded")


def test_keyless_default_fake_provider_completes(tmp_path: Path) -> None:
    rt = Runtime(Database(str(tmp_path / "atlas.db")), ROOT / "packages")  # no injection
    run_id = rt.create_run("assistant-agent", "say hi")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    assert run["output"]["message"].startswith("FAKE(")
