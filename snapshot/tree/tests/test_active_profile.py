"""Active assistant profile, run pinning, honest fallback accounting (M1.1 §1/§8)."""
from pathlib import Path

from atlas.db import Database
from atlas.profiles import ModelProfile, ProfileStore
from atlas.providers import FakeProvider, ProviderError, ProviderResult, ToolCall, Usage
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def two_provider_setup(tmp_path: Path):
    db = Database(str(tmp_path / "atlas.db"))
    store = ProfileStore(db)
    store.upsert(ModelProfile(id="prof-a", provider="prov_a", model="model-a"))
    store.upsert(ModelProfile(id="prof-b", provider="prov_b", model="model-b"))
    ask = ProviderResult(
        finish_reason="tool_use",
        text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "and?"}),),
    )
    done = ProviderResult(finish_reason="end", text="answered")
    prov_a = FakeProvider(script=[ask, done, done])
    prov_a.id = "prov_a"
    prov_b = FakeProvider(script=[done, done])
    prov_b.id = "prov_b"
    providers = {"fake": FakeProvider(), "prov_a": prov_a, "prov_b": prov_b}
    return db, store, providers, prov_a, prov_b


def test_active_profile_drives_new_assistant_runs(tmp_path: Path) -> None:
    db, store, providers, prov_a, prov_b = two_provider_setup(tmp_path)
    rt = Runtime(db, ROOT / "packages", providers=providers)

    store.set_active_profile("prof-a")
    run_a = rt.create_run("assistant-agent", "hello")
    assert rt.db.get_run(run_a)["state"] == "WAITING_FOR_USER"
    assert len(prov_a.requests) == 1 and prov_a.requests[0].model == "model-a"

    store.set_active_profile("prof-b")  # user switches in Settings
    run_b = rt.create_run("assistant-agent", "hi again")
    assert rt.db.get_run(run_b)["state"] == "COMPLETED"
    assert len(prov_b.requests) == 1 and prov_b.requests[0].model == "model-b"


def test_waiting_run_keeps_its_pinned_profile_across_switch_and_restart(tmp_path: Path) -> None:
    db, store, providers, prov_a, prov_b = two_provider_setup(tmp_path)
    rt = Runtime(db, ROOT / "packages", providers=providers)
    store.set_active_profile("prof-a")
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    assert run["context"]["llm_settings"]["provider"] == "prov_a"  # pinned

    store.set_active_profile("prof-b")
    restarted = Runtime(db, ROOT / "packages", providers=providers)  # process restart
    interaction = restarted.db.pending_interaction(run_id)
    restarted.answer(run_id, interaction["id"], "blue")

    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert len(prov_a.requests) == 2  # resumed on the PINNED provider
    assert len(prov_b.requests) == 0  # active-profile switch did not leak in


def test_fallback_counts_both_attempts_and_prices_with_fallback_profile(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    store = ProfileStore(db)
    store.upsert(
        ModelProfile(
            id="backup",
            provider="cheap",
            model="cheap-1",
            input_cost_per_mtok=1.0,      # $1 / Mtok
            output_cost_per_mtok=2.0,
        )
    )
    store.upsert(
        ModelProfile(
            id="pricey",
            provider="flaky",
            model="big-1",
            input_cost_per_mtok=1000.0,   # would give a very different cost
            output_cost_per_mtok=1000.0,
            fallback="backup",
        )
    )
    store.set_active_profile("pricey")
    flaky = FakeProvider(script=[ProviderError("rate_limit", "429")])
    flaky.id = "flaky"
    cheap = FakeProvider(
        script=[
            ProviderResult(
                finish_reason="end",
                text="saved",
                usage=Usage(input_tokens=1_000_000, output_tokens=500_000),
            )
        ]
    )
    cheap.id = "cheap"
    rt = Runtime(db, ROOT / "packages", providers={"fake": FakeProvider(), "flaky": flaky, "cheap": cheap})
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    context = run["context"]
    assert context["model_calls"] == 2                       # failed primary counted
    assert context["cost_usd"] == 2.0                        # 1M*$1 + 0.5M*$2, fallback pricing
    assert context["fallback_profile"]["id"] == "backup"     # snapshot pinned
    events = [e for e in rt.db.get_events(run_id) if e["type"] == "agent.fallback_used"]
    assert events and events[0]["payload"]["attempts"] == 2


def test_truncation_and_empty_responses_fail_loudly(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    truncated = FakeProvider(script=[ProviderResult(finish_reason="max_tokens", text="cut")])
    rt = Runtime(db, ROOT / "packages", providers={"fake": truncated})
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED" and "max_tokens" in run["output"]["error"]

    empty = FakeProvider(script=[ProviderResult(finish_reason="end", text="   ")])
    rt2 = Runtime(Database(str(tmp_path / "b.db")), ROOT / "packages", providers={"fake": empty})
    run_id = rt2.create_run("assistant-agent", "hello")
    run = rt2.db.get_run(run_id)
    assert run["state"] == "FAILED" and "empty final response" in run["output"]["error"]


def _fallback_pair(tmp_path: Path, *, max_calls: int, fallback_fails: bool = False):
    db = Database(str(tmp_path / "atlas.db"))
    store = ProfileStore(db)
    store.upsert(ModelProfile(id="backup", provider="cheap", model="cheap-1"))
    store.upsert(
        ModelProfile(
            id="pricey", provider="flaky", model="big-1",
            max_model_calls=max_calls, fallback="backup",
        )
    )
    store.set_active_profile("pricey")
    flaky = FakeProvider(script=[ProviderError("rate_limit", "429")])
    flaky.id = "flaky"
    cheap_script = (
        [ProviderError("timeout", "slow")] if fallback_fails
        else [ProviderResult(finish_reason="end", text="saved")]
    )
    cheap = FakeProvider(script=cheap_script)
    cheap.id = "cheap"
    rt = Runtime(db, ROOT / "packages",
                 providers={"fake": FakeProvider(), "flaky": flaky, "cheap": cheap})
    return rt, flaky, cheap


def test_budget_of_one_blocks_the_fallback_hop(tmp_path: Path) -> None:
    rt, flaky, cheap = _fallback_pair(tmp_path, max_calls=1)
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "no budget remains for the fallback" in run["output"]["error"]
    assert len(flaky.requests) == 1 and len(cheap.requests) == 0  # never called
    assert run["context"]["model_calls"] == 1                     # durable count
    attempts = [e for e in rt.db.get_events(run_id) if e["type"] == "agent.provider_attempt"]
    assert len(attempts) == 1 and attempts[0]["payload"]["ok"] is False


def test_budget_of_two_permits_primary_plus_fallback(tmp_path: Path) -> None:
    rt, flaky, cheap = _fallback_pair(tmp_path, max_calls=2)
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    assert run["context"]["model_calls"] == 2
    assert len(flaky.requests) == 1 and len(cheap.requests) == 1


def test_failed_fallback_still_records_two_attempts(tmp_path: Path) -> None:
    rt, flaky, cheap = _fallback_pair(tmp_path, max_calls=4, fallback_fails=True)
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert run["context"]["model_calls"] == 2                     # persisted before raise
    attempts = [e["payload"] for e in rt.db.get_events(run_id)
                if e["type"] == "agent.provider_attempt"]
    assert [a["ok"] for a in attempts] == [False, False]
    assert [a["provider"] for a in attempts] == ["flaky", "cheap"]


def test_resumed_run_cannot_reset_the_attempt_count(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    store = ProfileStore(db)
    store.upsert(ModelProfile(id="tight", provider="prov_a", model="m", max_model_calls=1))
    store.set_active_profile("tight")
    ask = ProviderResult(
        finish_reason="tool_use", text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "?"}),),
    )
    prov = FakeProvider(script=[ask, ProviderResult(finish_reason="end", text="x")])
    prov.id = "prov_a"
    rt = Runtime(db, ROOT / "packages", providers={"fake": FakeProvider(), "prov_a": prov})
    run_id = rt.create_run("assistant-agent", "hello")
    assert rt.db.get_run(run_id)["context"]["model_calls"] == 1   # persisted with the wait

    restarted = Runtime(db, ROOT / "packages",
                        providers={"fake": FakeProvider(), "prov_a": prov})
    interaction = restarted.db.pending_interaction(run_id)
    restarted.answer(run_id, interaction["id"], "blue")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "FAILED"                          # budget enforced on resume
    assert "Budget exhausted: 1 model calls" in finished["output"]["error"]
    assert len(prov.requests) == 1                                # no second call happened


def _pinned_fallback_setup(tmp_path: Path):
    db = Database(str(tmp_path / "atlas.db"))
    store = ProfileStore(db)
    store.upsert(ModelProfile(id="backup", provider="cheap", model="old"))
    store.upsert(ModelProfile(id="main", provider="flaky", model="big",
                              fallback="backup"))
    store.set_active_profile("main")
    ask = ProviderResult(
        finish_reason="tool_use", text="",
        tool_calls=(ToolCall(id="t1", name="user.ask", arguments={"question": "?"}),),
    )
    flaky = FakeProvider(script=[ask, ProviderError("rate_limit", "429")])
    flaky.id = "flaky"
    cheap = FakeProvider(script=[ProviderResult(finish_reason="end", text="saved"),
                                 ProviderResult(finish_reason="end", text="saved2")])
    cheap.id = "cheap"
    providers = {"fake": FakeProvider(), "flaky": flaky, "cheap": cheap}
    return db, store, providers, flaky, cheap


def test_paused_run_keeps_pinned_fallback_across_edit_and_restart(tmp_path: Path) -> None:
    db, store, providers, flaky, cheap = _pinned_fallback_setup(tmp_path)
    rt = Runtime(db, ROOT / "packages", providers=providers)
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    assert run["context"]["fallback_profile"]["model"] == "old"  # pinned at start

    store.upsert(ModelProfile(id="backup", provider="cheap", model="NEW"))

    restarted = Runtime(db, ROOT / "packages", providers=providers)  # + restart
    restarted.answer(run_id, restarted.db.pending_interaction(run_id)["id"], "blue")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert cheap.requests[0].model == "old"        # snapshot, not the live edit
    assert finished["context"]["fallback_profile"]["model"] == "old"


def test_new_run_uses_the_edited_fallback(tmp_path: Path) -> None:
    db, store, providers, flaky, cheap = _pinned_fallback_setup(tmp_path)
    store.upsert(ModelProfile(id="backup", provider="cheap", model="NEW"))
    flaky2 = FakeProvider(script=[ProviderError("rate_limit", "429")])
    flaky2.id = "flaky"
    providers["flaky"] = flaky2
    rt = Runtime(db, ROOT / "packages", providers=providers)
    run_id = rt.create_run("assistant-agent", "hello")
    assert rt.db.get_run(run_id)["state"] == "COMPLETED"
    assert cheap.requests[0].model == "NEW"


def test_deleting_the_live_fallback_does_not_break_a_pinned_run(tmp_path: Path) -> None:
    db, store, providers, flaky, cheap = _pinned_fallback_setup(tmp_path)
    rt = Runtime(db, ROOT / "packages", providers=providers)
    run_id = rt.create_run("assistant-agent", "hello")
    assert rt.db.get_run(run_id)["context"]["fallback_profile"]["id"] == "backup"

    # Unreference then delete the live profile entirely.
    store.upsert(ModelProfile(id="main", provider="flaky", model="big"))
    assert store.delete("backup") is True

    restarted = Runtime(db, ROOT / "packages", providers=providers)
    restarted.answer(run_id, restarted.db.pending_interaction(run_id)["id"], "blue")
    finished = restarted.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert cheap.requests[0].model == "old"        # served from the snapshot


def test_missing_fallback_fails_before_any_provider_call(tmp_path: Path) -> None:
    db, store, providers, flaky, cheap = _pinned_fallback_setup(tmp_path)
    rt = Runtime(db, ROOT / "packages", providers=providers)
    real_get = rt.profiles.get
    rt.profiles.get = lambda pid: None if pid == "backup" else real_get(pid)
    run_id = rt.create_run("assistant-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "Fallback profile not found at run start" in run["output"]["error"]
    assert len(flaky.requests) == 0 and len(cheap.requests) == 0
