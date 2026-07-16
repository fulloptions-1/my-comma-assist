"""Interaction continuations are atomic and crash-resumable (M1.2 §4/§8)."""
from pathlib import Path

from atlas.db import Database
from atlas.providers import FakeProvider, ProviderResult, ToolCall
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def test_mileage_answer_has_no_orphan_window(tmp_path: Path) -> None:
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages")
    run_id = rt.create_run("car-maintenance-agent", "log an oil change")
    mileage = rt.db.pending_interaction(run_id)
    rt.answer(run_id, mileage["id"], "142500")
    # ONE atomic step: resolved + already waiting on approval, no RUNNING hop.
    mid = rt.db.get_run(run_id)
    assert mid["state"] == "WAITING_FOR_APPROVAL"
    approval = rt.db.pending_interaction(run_id)
    assert approval is not None and approval["kind"] == "approval"

    # "Crash" immediately after: a restart must find a healthy waiting run.
    restarted = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages")
    stats = restarted.recover()
    assert stats["orphaned_failed"] == 0
    assert restarted.db.get_run(run_id)["state"] == "WAITING_FOR_APPROVAL"
    restarted.answer(run_id, approval["id"], True)
    assert restarted.db.get_run(run_id)["state"] == "COMPLETED"
    assert len(restarted.db.read_maintenance("genesis-2016")) == 1

    try:  # duplicate submission of the already-resolved mileage answer
        restarted.answer(run_id, mileage["id"], "142500")
    except ValueError:
        pass
    else:
        raise AssertionError("terminal run accepted another answer")


def _tie_packages(tmp_path: Path) -> Path:
    packages = tmp_path / "packages"
    if (packages / "p" / "manifest.yaml").exists():
        return packages
    (packages / "p" / "agents").mkdir(parents=True, exist_ok=True)
    (packages / "p" / "manifest.yaml").write_text(
        "id: p\nversion: 1\nagents:\n  - agents/concierge.yaml\n"
        "  - agents/left.yaml\n  - agents/right.yaml\n",
        encoding="utf-8",
    )
    (packages / "p" / "agents" / "concierge.yaml").write_text(
        "id: concierge-agent\nversion: 1\nname: C\ndescription: d\n"
        "handler: concierge\ntools: []\nintents: []\n",
        encoding="utf-8",
    )
    for name in ("left", "right"):
        (packages / "p" / "agents" / f"{name}.yaml").write_text(
            f"id: {name}-agent\nversion: 1\nname: {name}\ndescription: d\n"
            "handler: llm\ntools: []\nintents: [deploy]\n",
            encoding="utf-8",
        )
    return packages


def test_concierge_choice_crash_before_delegation_is_resumed(tmp_path: Path) -> None:
    packages = _tie_packages(tmp_path)
    db = Database(str(tmp_path / "a.db"))
    rt = Runtime(db, packages, providers={"fake": FakeProvider()})
    run_id = rt.create_run("auto", "deploy now")
    interaction = rt.db.pending_interaction(run_id)

    # Commit the decision atomically, then "crash" before delegation runs.
    context = rt.db.get_run(run_id)["context"]
    context["continuation"] = {"kind": "delegate", "target_id": "left-agent"}
    rt.db.resolve_interaction_and_transition(
        interaction["id"], "left-agent",
        run_id=run_id, expected_kind="user_input", new_state="RUNNING",
        context=context, event_type="run.resumed",
        payload={"source": "capability_choice"}, expected_state="WAITING_FOR_USER",
    )
    assert rt.db.get_run(run_id)["state"] == "RUNNING"

    done = FakeProvider(script=[ProviderResult(finish_reason="end", text="left did it")])
    restarted = Runtime(Database(str(tmp_path / "a.db")), packages, providers={"fake": done})
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert finished["output"]["message"] == "left did it"
    types = [e["type"] for e in restarted.db.get_events(run_id)]
    assert "run.continuation_resumed" in types
    delegated = [e for e in restarted.db.get_events(run_id) if e["type"] == "agent.delegated"]
    assert len(delegated) == 1  # exactly once
    for _ in range(3):
        restarted.recover()  # terminal run: repeated recovery changes nothing
    assert len([e for e in restarted.db.get_events(run_id)
                if e["type"] == "agent.delegated"]) == 1


def test_llm_answer_crash_before_provider_call_is_resumed(tmp_path: Path) -> None:
    ask = ProviderResult(
        finish_reason="tool_use", text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "?"}),),
    )
    fake = FakeProvider(script=[ask])
    db = Database(str(tmp_path / "a.db"))
    rt = Runtime(db, ROOT / "packages", providers={"fake": fake})
    run_id = rt.create_run("assistant-agent", "hello")
    interaction = rt.db.pending_interaction(run_id)

    # Commit the answer atomically (messages + continuation), crash pre-call.
    from atlas.providers import ProviderMessage
    context = rt.db.get_run(run_id)["context"]
    context["messages"].append(
        ProviderMessage("tool", "blue", tool_call_id="t1").to_dict()
    )
    context["continuation"] = {"kind": "llm"}
    rt.db.resolve_interaction_and_transition(
        interaction["id"], "blue",
        run_id=run_id, expected_kind="user_input", new_state="RUNNING",
        context=context, event_type="run.resumed",
        payload={"source": "user_input"}, expected_state="WAITING_FOR_USER",
        redact_answer=True,
    )

    resumed = FakeProvider(script=[ProviderResult(finish_reason="end", text="done")])
    restarted = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages",
                        providers={"fake": resumed})
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert finished["output"]["message"] == "done"
    # The resumed provider request carried the committed answer message.
    roles = [m.role for m in resumed.requests[0].messages]
    assert roles[-1] == "tool"
    assert "continuation" not in finished["context"]


def test_llm_answers_are_redacted_in_events(tmp_path: Path) -> None:
    ask = ProviderResult(
        finish_reason="tool_use", text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "key?"}),),
    )
    fake = FakeProvider(script=[ask, ProviderResult(finish_reason="end", text="ok")])
    rt = Runtime(Database(str(tmp_path / "a.db")), ROOT / "packages", providers={"fake": fake})
    run_id = rt.create_run("assistant-agent", "hello")
    interaction = rt.db.pending_interaction(run_id)
    rt.answer(run_id, interaction["id"], "sk-live-SECRET-9876")

    events = rt.db.get_events(run_id)
    resolved = [e for e in events if e["type"] == "user_input.resolved"]
    assert resolved and resolved[0]["payload"]["answer"] == "[redacted]"
    assert resolved[0]["payload"]["answer_length"] > 0
    import json
    assert "sk-live-SECRET-9876" not in json.dumps([e["payload"] for e in events])
    # The interaction ROW (functional record) keeps the real answer even
    # though the serializer does not expose it by default.
    with rt.db.read_conn() as conn:
        row = conn.execute(
            "SELECT answer_json FROM interactions WHERE id = ?", (interaction["id"],)
        ).fetchone()
    assert "sk-live-SECRET-9876" in row["answer_json"]
