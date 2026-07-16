"""Backup/restore via sqlite3.Connection.backup() (M1.2 §6)."""
import shutil
import sys
from pathlib import Path

from atlas.db import Database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_db  # noqa: E402


def test_backup_and_restore_roundtrip(tmp_path: Path) -> None:
    live = tmp_path / "atlas.db"
    db = Database(str(live))
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO maintenance_events
            (id, vehicle_id, event_type, occurred_at, odometer_km, notes,
             idempotency_key, created_at)
            VALUES ('m1', 'genesis-2016', 'oil_change', '2026-01-01',
                    142500, '', 'backup-test-1', '2026-01-01T00:00:00+00:00')"""
        )

    dest = tmp_path / "backups" / "atlas-backup.db"
    backup_db.backup(str(live), str(dest))
    assert dest.exists()

    restored_path = tmp_path / "restored.db"
    shutil.copy(dest, restored_path)  # the documented restore: copy back
    restored = Database(str(restored_path))
    rows = restored.read_maintenance("genesis-2016")
    assert len(rows) == 1 and rows[0]["odometer_km"] == 142500


def test_backup_refuses_to_overwrite(tmp_path: Path) -> None:
    live = tmp_path / "atlas.db"
    Database(str(live))
    dest = tmp_path / "existing.db"
    dest.write_text("do not clobber", encoding="utf-8")
    try:
        backup_db.backup(str(live), str(dest))
    except SystemExit as exc:
        assert "refusing" in str(exc)
    else:
        raise AssertionError("overwrote an existing backup file")
