#!/usr/bin/env python3
"""Online backup of the Atlas SQLite database — pure Python (M1.2 §6).

The python:3.12-slim image does NOT ship the sqlite3 command-line tool, so
the previously documented `sqlite3 .backup` command could never run there.
This script uses sqlite3.Connection.backup(), which is safe against a live
WAL-mode database, and verifies the copy with PRAGMA integrity_check.

Backup:   python scripts/backup_db.py /data/atlas.db /data/backups/atlas-2026-07-15.db
Restore:  stop the service, copy the backup file over ATLAS_DB_PATH
          (also remove stale <db>-wal / <db>-shm siblings), start the service.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def backup(src: str, dst: str) -> None:
    src_path = Path(src)
    if not src_path.exists():
        raise SystemExit(f"source database not found: {src}")
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        raise SystemExit(f"refusing to overwrite existing backup: {dst}")

    source = sqlite3.connect(src)
    target = sqlite3.connect(dst)
    try:
        with target:
            source.backup(target)  # atomic online copy, WAL-safe
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"backup FAILED integrity check: {result}")
    finally:
        source.close()
        target.close()
    print(f"backup ok: {dst} (integrity_check: ok)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="path to the live database, e.g. /data/atlas.db")
    parser.add_argument("destination", help="path for the backup file")
    args = parser.parse_args(argv)
    backup(args.source, args.destination)


if __name__ == "__main__":
    main(sys.argv[1:])
