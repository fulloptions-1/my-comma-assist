"""Numbered migration mechanism (M1-B)."""
import sqlite3
from pathlib import Path

from atlas import migrations
from atlas.db import Database
from atlas.migrations import MigrationError, apply_migrations, applied_versions


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_clean_database_gets_all_migrations(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    with db.read_conn() as conn:
        assert applied_versions(conn) == {m[0] for m in migrations.MIGRATIONS}
        names = table_names(conn)
    for expected in (
        "runs", "run_events", "interactions", "tool_executions",
        "maintenance_events", "resource_locks", "settings",
        "sessions", "config", "schema_migrations",
    ):
        assert expected in names, expected


def test_upgrade_from_original_vertical_slice_schema(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = raw(path)
    for statement in migrations.MIGRATIONS[0][2]:  # original schema only
        conn.execute(statement)
    conn.execute(
        "INSERT INTO runs VALUES ('r1','agent','a',1,'COMPLETED','{}','{}','{}',0,'t','t')"
    )
    conn.commit()
    conn.close()

    db = Database(str(path))  # constructor migrates
    with db.read_conn() as conn:
        assert applied_versions(conn) == {1, 2, 3, 4}
        assert {"resource_locks", "settings", "sessions", "config"} <= table_names(conn)
        row = conn.execute("SELECT id, state FROM runs").fetchone()
        assert (row["id"], row["state"]) == ("r1", "COMPLETED")  # data preserved


def test_upgrade_from_s1_s7_schema_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "s7.db"
    conn = raw(path)
    for _, _, statements in migrations.MIGRATIONS[:2]:  # 0001 + 0002, no ledger
        for statement in statements:
            conn.execute(statement)
    conn.execute(
        "INSERT INTO settings VALUES ('k', X'00ff', 't')"
    )
    conn.commit()
    conn.close()

    db = Database(str(path))
    with db.read_conn() as conn:
        assert applied_versions(conn) == {1, 2, 3, 4}
        stored = conn.execute("SELECT value_encrypted FROM settings").fetchone()[0]
        assert bytes(stored) == b"\x00\xff"


def test_failed_migration_rolls_back_atomically(tmp_path: Path) -> None:
    path = tmp_path / "fail.db"
    conn = raw(path)
    original = list(migrations.MIGRATIONS)
    migrations.MIGRATIONS = original + [
        (
            99,
            "boom",
            (
                "CREATE TABLE half_applied (id TEXT PRIMARY KEY)",
                "THIS IS NOT SQL",
            ),
        )
    ]
    try:
        try:
            apply_migrations(conn)
        except MigrationError as exc:
            assert "0099_boom" in str(exc) and "rolled back" in str(exc)
        else:
            raise AssertionError("bad migration did not raise")
        assert "half_applied" not in table_names(conn)  # DDL rolled back
        assert 99 not in applied_versions(conn)         # version not recorded
        assert applied_versions(conn) == {1, 2, 3, 4}   # earlier ones intact
    finally:
        migrations.MIGRATIONS = original
        conn.close()


def test_reapply_is_a_recorded_noop(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    with db.read_conn() as conn:
        assert apply_migrations(conn) == []  # nothing pending
        assert migrations.current_version(conn) == migrations.latest_version()
