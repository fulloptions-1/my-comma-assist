"""Event-specific maintenance queries return the right event (M1.4 §4)."""
from pathlib import Path

from atlas.db import Database
from atlas.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]

SEED = [
    ("m1", "oil_change", "2026-01-05", 100_000),
    ("m2", "insurance_renewal", "2026-02-01", 104_000),
    ("m3", "tire_replacement", "2026-03-10", 110_000),
    ("m4", "maintenance_service", "2026-04-02", 114_000),
    ("m5", "oil_and_filter_change", "2026-05-20", 118_000),
]


def seeded(tmp_path: Path) -> Runtime:
    db = Database(str(tmp_path / "a.db"))
    with db.transaction() as conn:
        for row_id, event_type, occurred, km in SEED:
            conn.execute(
                """INSERT INTO maintenance_events
                (id, vehicle_id, event_type, occurred_at, odometer_km, notes,
                 idempotency_key, created_at)
                VALUES (?, 'genesis-2016', ?, ?, ?, '', ?, ?)""",
                (row_id, event_type, occurred, km, f"seed-{row_id}",
                 f"{occurred}T00:00:00+00:00"),
            )
    return Runtime(db, ROOT / "packages")


def ask(rt: Runtime, message: str) -> dict:
    run_id = rt.create_run("car-maintenance-agent", message)
    run = rt.db.get_run(run_id)
    assert run["state"] == "COMPLETED", run["output"]
    return run["output"]


def test_last_oil_change_spans_the_oil_category(tmp_path: Path) -> None:
    """M1.5 §2: an oil-and-filter service is also an oil change, so the
    category question returns the NEWEST of both types — while still never
    returning the tire job in between."""
    out = ask(seeded(tmp_path), "When was my last oil change?")
    assert out["events"][0]["event_type"] == "oil_and_filter_change"
    assert out["events"][0]["odometer_km"] == 118_000
    types = {e["event_type"] for e in out["events"]}
    assert types == {"oil_change", "oil_and_filter_change"}
    assert "tire_replacement" not in types
    assert "118,000" in out["message"]


def test_oil_category_without_filter_service_finds_plain_oil_change(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "solo.db"))
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO maintenance_events
            (id, vehicle_id, event_type, occurred_at, odometer_km, notes,
             idempotency_key, created_at)
            VALUES ('s1', 'genesis-2016', 'oil_and_filter_change', '2026-06-01',
                    120000, '', 'solo-1', '2026-06-01T00:00:00+00:00')"""
        )
    rt = Runtime(db, ROOT / "packages")
    out = ask(rt, "When was my last oil change?")   # the audit's exact repro
    assert out["events"] and out["events"][0]["event_type"] == "oil_and_filter_change"
    assert out["events"][0]["odometer_km"] == 120_000


def test_each_named_event_type_filters(tmp_path: Path) -> None:
    rt = seeded(tmp_path)
    cases = [
        ("When did I last replace my tires?", "tire_replacement", 110_000),
        ("when was my insurance renewal?", "insurance_renewal", 104_000),
        ("when was my last oil and filter change?", "oil_and_filter_change", 118_000),  # exact, not category
        ("when was my last service?", "maintenance_service", 114_000),
    ]
    for message, expected_type, expected_km in cases:
        out = ask(rt, message)
        assert out["events"], message
        assert out["events"][0]["event_type"] == expected_type, message
        assert out["events"][0]["odometer_km"] == expected_km, message


def test_generic_history_remains_unfiltered(tmp_path: Path) -> None:
    out = ask(seeded(tmp_path), "show my maintenance history")
    assert len(out["events"]) == len(SEED)
    assert out["events"][0]["event_type"] == "oil_and_filter_change"  # newest first


def test_filtered_empty_history_says_which_type(tmp_path: Path) -> None:
    rt = Runtime(Database(str(tmp_path / "empty.db")), ROOT / "packages")
    run_id = rt.create_run("car-maintenance-agent", "when was my last oil change?")
    out = rt.db.get_run(run_id)["output"]
    assert out["events"] == []
    assert "oil change" in out["message"].lower()


def test_db_filter_is_exact_and_ordered(tmp_path: Path) -> None:
    rt = seeded(tmp_path)
    only_oil = rt.db.read_maintenance("genesis-2016", event_type="oil_change")
    assert [r["id"] for r in only_oil] == ["m1"]
    category = rt.db.read_maintenance(
        "genesis-2016", event_types=("oil_change", "oil_and_filter_change")
    )
    assert [r["id"] for r in category] == ["m5", "m1"]  # newest first, both types
    everything = rt.db.read_maintenance("genesis-2016")
    assert [r["id"] for r in everything] == ["m5", "m4", "m3", "m2", "m1"]
