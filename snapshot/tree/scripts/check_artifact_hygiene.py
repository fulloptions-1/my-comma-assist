#!/usr/bin/env python3
"""Fail loudly if a release zip contains generated caches or secrets (M1.1 §12).

Usage: python3 scripts/check_artifact_hygiene.py <tree.zip> [more.zip ...]
Exit 0 = clean; exit 1 = violations listed on stderr.
"""
import sys
import zipfile

FORBIDDEN_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite")
FORBIDDEN_NAMES = {".env"}


def violations(path: str) -> list[str]:
    bad: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            parts = name.split("/")
            leaf = parts[-1] or (parts[-2] if len(parts) > 1 else "")
            if any(part in FORBIDDEN_PARTS for part in parts):
                bad.append(name)
            elif leaf in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES):
                bad.append(name)
    return bad


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    dirty = False
    for path in argv[1:]:
        bad = violations(path)
        if bad:
            dirty = True
            print(f"HYGIENE VIOLATIONS in {path}:", file=sys.stderr)
            for name in sorted(bad):
                print(f"  {name}", file=sys.stderr)
        else:
            print(f"clean: {path}")
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
