"""Effective-agent pinning for delegated and direct runs (M1.2 §1)."""
import shutil
from pathlib import Path

from atlas.db import Database
from atlas.providers import FakeProvider, ProviderResult, ToolCall
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_PROMPT = "You are Atlas, a concise assistant."


def ask_then_done() -> FakeProvider:
    ask = ProviderResult(
        finish_reason="tool_use",
        text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "color?"}),),
    )
    done = ProviderResult(finish_reason="end", text="thanks")
    return FakeProvider(script=[ask, done])


def test_auto_delegated_assistant_resumes_with_assistant_blueprint(tmp_path: Path) -> None:
    fake = ask_then_done()
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages", providers={"fake": fake})
    run_id = rt.create_run("auto", "Hello Atlas")  # concierge -> assistant
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    assert run["target_id"] == "concierge-agent"                 # entry preserved
    assert run["context"]["effective_agent"]["id"] == "assistant-agent"

    rt.answer(run_id, rt.db.pending_interaction(run_id)["id"], "blue")
    assert rt.db.get_run(run_id)["state"] == "COMPLETED"
    assert len(fake.requests) == 2
    first, resumed = fake.requests
    assert ASSISTANT_PROMPT in (first.system or "")
    assert (resumed.system or "") == (first.system or "")        # SAME prompt on resume
    resumed_tools = {t.name for t in (resumed.tools or ())}
    assert "car.read_history" in resumed_tools                   # assistant tool set


def test_delegated_identity_survives_process_restart(tmp_path: Path) -> None:
    fake = ask_then_done()
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages", providers={"fake": fake})
    run_id = rt.create_run("auto", "Hello Atlas")

    fake2 = FakeProvider(script=[ProviderResult(finish_reason="end", text="after restart")])
    restarted = Runtime(
        Database(str(tmp_path / "a.db")), ROOT / "packages", providers={"fake": fake2}
    )
    restarted.answer(run_id, restarted.db.pending_interaction(run_id)["id"], "blue")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert ASSISTANT_PROMPT in (fake2.requests[0].system or "")
    assert finished["target_id"] == "concierge-agent"


def test_direct_runs_pin_their_effective_agent(tmp_path: Path) -> None:
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages",
                 providers={"fake": ask_then_done()})
    llm_run = rt.create_run("assistant-agent", "hi")
    assert rt.db.get_run(llm_run)["context"]["effective_agent"]["id"] == "assistant-agent"

    car_run = rt.create_run("car-maintenance-agent", "log an oil change")
    car = rt.db.get_run(car_run)
    assert car["context"]["effective_agent"]["id"] == "car-maintenance-agent"
    assert car["context"]["effective_agent"]["handler"] == "car_maintenance"


def test_definition_drift_while_paused_fails_loudly(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    shutil.copytree(ROOT / "packages", packages)
    fake = ask_then_done()
    rt = Runtime(Database(str(tmp_path / "a.db")), packages, providers={"fake": fake})
    run_id = rt.create_run("assistant-agent", "hi")
    assert rt.db.get_run(run_id)["state"] == "WAITING_FOR_USER"

    yaml_path = packages / "core" / "agents" / "assistant.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace(
            "concise assistant", "VERBOSE assistant"
        ),
        encoding="utf-8",
    )
    restarted = Runtime(Database(str(tmp_path / "a.db")), packages, providers={"fake": fake})
    interaction = restarted.db.pending_interaction(run_id)
    try:
        restarted.answer(run_id, interaction["id"], "blue")
    except ValueError as exc:
        assert "changed while the run was in flight" in str(exc)
    else:
        raise AssertionError("resumed silently against a changed definition")
    failed = restarted.db.get_run(run_id)
    assert failed["state"] == "FAILED"
    assert "changed" in failed["output"]["error"]
    assert len(fake.requests) == 1  # the resumed call never happened
