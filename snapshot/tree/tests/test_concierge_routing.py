"""Auto routing: general chat, specialists, ambiguity (M1.1 §2)."""
from pathlib import Path

from atlas.db import Database
from atlas.providers import FakeProvider, ProviderResult
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def make_runtime(tmp_path: Path, packages: Path | None = None) -> Runtime:
    fake = FakeProvider(script=[ProviderResult(finish_reason="end", text="Hi! I'm Atlas.")])
    return Runtime(
        Database(str(tmp_path / "atlas.db")),
        packages or ROOT / "packages",
        providers={"fake": fake},
    )


def test_general_prompt_routes_to_assistant(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    run_id = rt.create_run("auto", "Hello Atlas")
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED"
    assert run["output"]["message"] == "Hi! I'm Atlas."
    delegated = [e for e in rt.db.get_events(run_id) if e["type"] == "agent.delegated"]
    assert delegated and delegated[0]["payload"]["target_id"] == "assistant-agent"


def test_specialist_match_still_wins(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)
    run_id = rt.create_run("auto", "I need an oil change soon")
    run = rt.db.get_run(run_id)
    # An unambiguous specialist is resolved directly at run creation.
    assert run["target_id"] == "car-maintenance-agent"
    assert run["state"] == "WAITING_FOR_USER"  # car flow asks for mileage


def ambiguous_packages(tmp_path: Path) -> Path:
    packages = tmp_path / "packages"
    (packages / "p" / "agents").mkdir(parents=True, exist_ok=True)
    if (packages / "p" / "manifest.yaml").exists():
        return packages
    (packages / "p" / "manifest.yaml").write_text(
        "id: p\nversion: 1\nagents:\n"
        "  - agents/concierge.yaml\n  - agents/left.yaml\n  - agents/right.yaml\n",
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


def test_ambiguous_tie_asks_then_delegates_choice(tmp_path: Path) -> None:
    ask_then = FakeProvider(script=[ProviderResult(finish_reason="end", text="left did it")])
    rt = Runtime(
        Database(str(tmp_path / "atlas.db")),
        ambiguous_packages(tmp_path),
        providers={"fake": ask_then},
    )
    run_id = rt.create_run("auto", "please deploy this")
    run = rt.db.get_run(run_id)
    assert run["state"] == "WAITING_FOR_USER"
    interaction = rt.db.pending_interaction(run_id)
    assert interaction["payload"]["options"] == ["left-agent", "right-agent"]

    try:
        rt.answer(run_id, interaction["id"], "nonsense-agent")
    except ValueError as exc:
        assert "Choose one of" in str(exc)
    else:
        raise AssertionError("invalid choice accepted")
    assert rt.db.pending_interaction(run_id) is not None  # still answerable

    rt.answer(run_id, interaction["id"], "left-agent")
    finished = rt.db.get_run(run_id)
    assert finished["state"] == "COMPLETED"
    assert finished["output"]["message"] == "left did it"


def test_lost_choice_interaction_is_reissued_on_recovery(tmp_path: Path) -> None:
    rt = Runtime(
        Database(str(tmp_path / "atlas.db")),
        ambiguous_packages(tmp_path),
        providers={"fake": FakeProvider()},
    )
    run_id = rt.create_run("auto", "deploy now")
    interaction = rt.db.pending_interaction(run_id)
    with rt.db.transaction() as conn:  # simulate the historical lost-interaction wedge
        conn.execute("DELETE FROM interactions WHERE id = ?", (interaction["id"],))
    restarted = Runtime(
        Database(str(tmp_path / "atlas.db")),
        ambiguous_packages(tmp_path),
        providers={"fake": FakeProvider(script=[ProviderResult(finish_reason="end", text="ok")])},
    )
    reissued = restarted.db.pending_interaction(run_id)
    assert reissued is not None and reissued["payload"]["resume"] == "concierge_choice"
    restarted.answer(run_id, reissued["id"], "right-agent")
    assert restarted.db.get_run(run_id)["state"] == "COMPLETED"
