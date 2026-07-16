"""Model profiles: store, resolution precedence, fallback, cost budgets (M1-E)."""
from pathlib import Path

from atlas.db import Database
from atlas.profiles import DEFAULT_PROFILES, ModelProfile, ProfileError, ProfileStore
from atlas.providers import FakeProvider, ProviderError, ProviderResult, Usage
from atlas.registry import PackageValidationError
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def write_llm_agent(packages: Path, agent_yaml: str) -> None:
    (packages / "p" / "agents").mkdir(parents=True)
    (packages / "p" / "manifest.yaml").write_text(
        "id: p\nversion: 1\nagents:\n  - agents/a.yaml\n", encoding="utf-8"
    )
    (packages / "p" / "agents" / "a.yaml").write_text(agent_yaml, encoding="utf-8")


BASE_AGENT = """id: prof-agent
version: 1
name: P
description: d
handler: llm
tools: []
intents: [p]
"""


def test_store_roundtrip_defaults_and_guards(tmp_path: Path) -> None:
    store = ProfileStore(Database(str(tmp_path / "atlas.db")))
    assert set(DEFAULT_PROFILES) <= set(store.list())

    cheap = ModelProfile.from_dict(
        {
            "id": "cheap",
            "provider": "openai_local",
            "model": "m-1",
            "max_tokens": 256,
            "input_cost_per_mtok": 0.1,
            "output_cost_per_mtok": 0.4,
            "cost_budget_usd": 0.01,
            "fallback": "fake-default",
        }
    )
    store.upsert(cheap)
    fresh = ProfileStore(Database(str(tmp_path / "atlas.db")))
    assert fresh.get("cheap") == cheap  # survives restart via config table

    for bad, needle in (
        ({"id": "x", "provider": "p", "surprise": 1}, "unknown profile fields"),
        ({"id": "", "provider": "p"}, "id is required"),
        ({"id": "x", "provider": ""}, "provider is required"),
        ({"id": "x", "provider": "p", "fallback": "x"}, "fall back to itself"),
    ):
        try:
            ModelProfile.from_dict(bad)
        except ProfileError as exc:
            assert needle in str(exc)
        else:
            raise AssertionError(f"accepted {bad}")

    try:
        store.upsert(ModelProfile(id="y", provider="p", fallback="nope"))
    except ProfileError as exc:
        assert "fallback profile not found" in str(exc)
    else:
        raise AssertionError("dangling fallback accepted")

    assert store.delete("cheap") is True and store.get("cheap") is None
    store.upsert(ModelProfile(id="tmp-active", provider="p"))
    store.set_active_profile("tmp-active")
    try:
        store.delete("fake-default")
    except ProfileError as exc:
        assert "built-in" in str(exc)
    else:
        raise AssertionError("deleted a built-in profile")
    store.set_active_profile("fake-default")
    assert store.delete("tmp-active") is True


def test_agent_fields_override_profile_values(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    write_llm_agent(
        packages,
        BASE_AGENT + "profile: fake-default\nmodel: agent-wins\nmax_model_calls: 2\n",
    )
    fake = FakeProvider()  # default echo mode records requests
    rt = Runtime(Database(str(tmp_path / "atlas.db")), packages, providers={"fake": fake})
    run_id = rt.create_run("prof-agent", "hello")
    assert rt.db.get_run(run_id)["state"] == "COMPLETED"
    assert fake.requests[0].model == "agent-wins"  # agent field beat profile's fake-1
    assert fake.requests[0].max_tokens == 1024


def test_fallback_on_retryable_error_records_event(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    ProfileStore(db).upsert(
        ModelProfile(id="primary", provider="flaky", model="big", fallback="fake-default")
    )
    packages = tmp_path / "packages"
    write_llm_agent(packages, BASE_AGENT + "profile: primary\n")

    flaky = FakeProvider(script=[ProviderError("rate_limit", "429")])
    flaky.id = "flaky"
    backup = FakeProvider(
        script=[ProviderResult(finish_reason="end", text="saved by fallback")]
    )
    rt = Runtime(db, packages, providers={"fake": backup, "flaky": flaky})
    run_id = rt.create_run("prof-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    assert run["output"]["message"] == "saved by fallback"
    types = [e["type"] for e in rt.db.get_events(run_id)]
    assert "agent.fallback_used" in types
    assert backup.requests[0].model == "fake-1"  # fallback profile's model applied


def test_auth_error_does_not_fall_back(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    ProfileStore(db).upsert(
        ModelProfile(id="primary", provider="flaky", fallback="fake-default")
    )
    packages = tmp_path / "packages"
    write_llm_agent(packages, BASE_AGENT + "profile: primary\n")
    flaky = FakeProvider(script=[ProviderError("auth", "401 bad key")])
    flaky.id = "flaky"
    rt = Runtime(db, packages, providers={"fake": FakeProvider(), "flaky": flaky})
    run_id = rt.create_run("prof-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "[auth]" in run["output"]["error"]
    assert "agent.fallback_used" not in [e["type"] for e in rt.db.get_events(run_id)]


def test_cost_budget_fails_loudly_with_user_pricing(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    ProfileStore(db).upsert(
        ModelProfile(
            id="priced",
            provider="fake",
            input_cost_per_mtok=1000.0,   # user-supplied test pricing
            output_cost_per_mtok=1000.0,
            cost_budget_usd=0.5,
        )
    )
    packages = tmp_path / "packages"
    write_llm_agent(packages, BASE_AGENT + "profile: priced\n")
    # one call: 400 in + 200 out at $1000/Mtok => $0.6 > $0.5 budget
    provider = FakeProvider(
        script=[
            ProviderResult(
                finish_reason="end", text="hi", usage=Usage(input_tokens=400, output_tokens=200)
            )
        ]
    )
    rt = Runtime(db, packages, providers={"fake": provider})
    run_id = rt.create_run("prof-agent", "hello")
    run = rt.db.get_run(run_id)
    assert run["state"] == "FAILED"
    assert "cost $0.6" in run["output"]["error"]
    assert run["context"]["cost_usd"] == 0.6
    model_events = [e for e in rt.db.get_events(run_id) if e["type"] == "agent.model_called"]
    assert model_events[-1]["payload"]["cost_usd"] == 0.6


def test_unknown_profile_reference_rejected_at_load(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    write_llm_agent(packages, BASE_AGENT + "profile: does-not-exist\n")
    try:
        Runtime(Database(str(tmp_path / "atlas.db")), packages)
    except PackageValidationError as exc:
        assert "unknown model profile" in str(exc)
    else:
        raise AssertionError("agent with unknown profile loaded")


def test_numeric_length_and_chain_validation(tmp_path: Path) -> None:
    store = ProfileStore(Database(str(tmp_path / "atlas.db")))
    bad_cases = [
        ({"id": "x", "provider": "p", "timeout_s": 0}, "timeout_s"),
        ({"id": "x", "provider": "p", "max_model_calls": 0}, "max_model_calls"),
        ({"id": "x", "provider": "p", "max_tool_calls": -1}, "max_tool_calls"),
        ({"id": "x", "provider": "p", "input_cost_per_mtok": -0.1}, "input_cost_per_mtok"),
        ({"id": "x", "provider": "p", "cost_budget_usd": 0}, "cost_budget_usd"),
        ({"id": "x" * 65, "provider": "p"}, "id exceeds"),
        ({"id": "x", "provider": "p", "model": "m" * 129}, "model exceeds"),
    ]
    for payload, needle in bad_cases:
        try:
            ModelProfile.from_dict(payload)
        except ProfileError as exc:
            assert needle in str(exc), (payload, str(exc))
        else:
            raise AssertionError(f"accepted {payload}")

    store.upsert(ModelProfile(id="a", provider="p", fallback="fake-default"))
    try:  # chain: b -> a -> fake-default
        store.upsert(ModelProfile(id="b", provider="p", fallback="a"))
    except ProfileError as exc:
        assert "one hop only" in str(exc)
    else:
        raise AssertionError("fallback chain accepted")
    try:  # would retro-create chain: a is a target of nothing yet, but make c->a then a->x
        store.upsert(ModelProfile(id="c", provider="p", fallback="a"))
        raise AssertionError("chain via existing fallback accepted")
    except ProfileError as exc:
        assert "one hop only" in str(exc)


def test_delete_guards_active_and_referenced(tmp_path: Path) -> None:
    store = ProfileStore(Database(str(tmp_path / "atlas.db")))
    store.upsert(ModelProfile(id="solo", provider="p"))
    store.upsert(ModelProfile(id="uses-solo", provider="p", fallback="solo"))
    try:
        store.delete("solo")
    except ProfileError as exc:
        assert "fallback of" in str(exc)
    else:
        raise AssertionError("deleted a referenced fallback target")

    store.set_active_profile("uses-solo")
    try:
        store.delete("uses-solo")
    except ProfileError as exc:
        assert "active" in str(exc)
    else:
        raise AssertionError("deleted the active profile")
    store.set_active_profile("fake-default")
    assert store.delete("uses-solo") is True
    assert store.delete("solo") is True
    try:
        store.set_active_profile("ghost")
    except ProfileError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("activated unknown profile")


def test_types_finiteness_and_upper_bounds(tmp_path: Path) -> None:
    cases = [
        ({"id": "x", "provider": "p", "max_tokens": "x"}, "integer"),
        ({"id": "x", "provider": "p", "max_tokens": True}, "integer"),
        ({"id": "x", "provider": "p", "max_tokens": 10 ** 100}, "between"),
        ({"id": "x", "provider": "p", "max_tokens": 5.0}, "integer"),
        ({"id": "x", "provider": "p", "max_model_calls": 2.0}, "integer"),
        ({"id": "x", "provider": "p", "max_tool_calls": 1.5}, "integer"),
        ({"id": "x", "provider": "p", "max_model_calls": False}, "integer"),
        ({"id": "x", "provider": "p", "max_model_calls": 999_999}, "between"),
        ({"id": "x", "provider": "p", "timeout_s": "5"}, "number"),
        ({"id": "x", "provider": "p", "timeout_s": float("inf")}, "finite"),
        ({"id": "x", "provider": "p", "timeout_s": 999_999.0}, "<="),
        ({"id": "x", "provider": "p", "input_cost_per_mtok": float("nan")}, "finite"),
        ({"id": "x", "provider": "p", "cost_budget_usd": float("inf")}, "finite"),
        ({"id": "x", "provider": "p", "cost_budget_usd": 5_000_000}, "<="),
        ({"id": "x", "provider": "p", "model": 7}, "string"),
        ({"id": "x", "provider": "p", "bogus_field": 1}, "unknown"),
    ]
    for payload, needle in cases:
        try:
            ModelProfile.from_dict(payload)
        except ProfileError as exc:
            assert needle in str(exc), (payload, str(exc))
        else:
            raise AssertionError(f"accepted {payload}")


def test_upsert_validates_directly_constructed_profiles(tmp_path: Path) -> None:
    store = ProfileStore(Database(str(tmp_path / "atlas.db")))
    try:
        store.upsert(ModelProfile(id="huge", provider="p", max_tokens=10 ** 100))
    except ProfileError as exc:
        assert "max_tokens" in str(exc)
    else:
        raise AssertionError("upsert accepted an absurd direct-constructed profile")
    assert store.get("huge") is None
