"""Production preflight and readiness checks (pure; wired in atlas/app.py).

Development mode stays convenient and keyless. Production mode
(ATLAS_ENV=production) refuses to start half-secured: a missing access
token or encryption key is fatal, and a database outside the durable /data
volume is loudly warned about.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from atlas import migrations
from atlas.db import Database


@dataclass
class Preflight:
    env: str
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    storage: str = "unconfirmed"  # mounted | ephemeral-acknowledged | unconfirmed
    storage_evidence: dict = field(default_factory=dict)  # M1.3 §3: what was checked

    @property
    def production(self) -> bool:
        return self.env == "production"


def storage_durability(
    db_path: str,
    *,
    volume_root: str = "/data",
    ack_ephemeral: bool = False,
    is_mount=os.path.ismount,
) -> dict:
    """Classify database storage durability against an EXPLICIT volume root
    (M1.3 §3).

    The M1.2 version walked every ancestor of the db directory; since `/`
    is always a mount point on Linux, virtually every absolute path was
    classified as durable — false assurance. Now:

    - the operator declares where the persistent volume is expected
      (ATLAS_VOLUME_PATH, default /data);
    - the database must live INSIDE that root;
    - the volume root itself, or a non-root ancestor of the db directory
      beneath it, must be a real mount point;
    - the container root filesystem `/` is NEVER evidence of persistence.

    Returns evidence, not just a verdict:
      {"state": "mounted" | "ephemeral-acknowledged" | "unconfirmed",
       "volume_root": ..., "db_dir": ..., "mounted_at": path-or-None,
       "reason": ...}
    ack_ephemeral acknowledges ANY non-mounted condition (including a db
    outside the declared root): the operator explicitly accepts data loss;
    the reason field preserves what was actually detected."""
    root = Path(volume_root).expanduser().resolve()
    directory = Path(db_path).expanduser().resolve().parent
    evidence = {
        "state": "unconfirmed",
        "volume_root": str(root),
        "db_dir": str(directory),
        "mounted_at": None,
        "reason": "",
    }
    if root == Path("/"):
        evidence["reason"] = "volume_root_is_filesystem_root"
    elif not directory.is_relative_to(root):
        evidence["reason"] = "database_outside_volume_root"
    else:
        candidates = [directory, *directory.parents]
        candidates = [c for c in candidates if c.is_relative_to(root) and c != Path("/")]
        for candidate in candidates:  # deepest first, ending at volume root
            try:
                if is_mount(str(candidate)):
                    evidence["state"] = "mounted"
                    evidence["mounted_at"] = str(candidate)
                    evidence["reason"] = "mount_point_detected"
                    return evidence
            except OSError:
                break
        evidence["reason"] = "volume_root_not_mounted"
    if ack_ephemeral:
        evidence["state"] = "ephemeral-acknowledged"
    return evidence


def run_preflight(
    env: str,
    access_token: str,
    secret_key: str,
    db_path: str,
    *,
    volume_root: str | None = None,
    ack_ephemeral: bool = False,
    is_mount=os.path.ismount,
) -> Preflight:
    report = Preflight(env=env)
    production = report.production
    volume_root = volume_root or os.getenv("ATLAS_VOLUME_PATH", "/data")

    if not access_token:
        message = (
            "ATLAS_ACCESS_TOKEN is not set: the API would be OPEN to anyone."
        )
        (report.fatal if production else report.warnings).append(
            message + (" Refusing to start in production." if production else " (development mode)")
        )
    if not secret_key:
        message = "ATLAS_SECRET_KEY is not set: encrypted provider settings are disabled."
        (report.fatal if production else report.warnings).append(
            message + (" Refusing to start in production." if production else " (development mode)")
        )

    directory = Path(db_path).expanduser().resolve().parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".atlas-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.fatal.append(f"Database path is not writable: {directory} ({exc})")

    evidence = storage_durability(
        db_path, volume_root=volume_root,
        ack_ephemeral=ack_ephemeral, is_mount=is_mount,
    )
    report.storage = evidence["state"]
    report.storage_evidence = evidence
    if production and report.storage == "unconfirmed":
        if evidence["reason"] == "database_outside_volume_root":
            report.fatal.append(
                f"Durable storage is UNCONFIRMED: the database directory "
                f"{evidence['db_dir']} is OUTSIDE the configured volume root "
                f"{evidence['volume_root']} (ATLAS_VOLUME_PATH). Move the "
                "database under the volume, or set "
                "ATLAS_ACK_EPHEMERAL_STORAGE=1 to knowingly accept data loss."
            )
        else:
            report.fatal.append(
                f"Durable storage is UNCONFIRMED: {evidence['volume_root']} is "
                "not a mount point, so the database will be lost on redeploy "
                f"(reason: {evidence['reason']}). Attach a volume (on Railway: "
                "service → Volumes → mount path /data), or set "
                "ATLAS_ACK_EPHEMERAL_STORAGE=1 to knowingly accept data loss."
            )
    elif not production and report.storage == "unconfirmed":
        report.warnings.append(
            f"Storage is not confirmed durable ({evidence['reason']}); data "
            "may not survive a redeploy (fine for development)."
        )
    return report


def readiness(
    db: Database,
    agents_loaded: int,
    storage: str = "unconfirmed",
    storage_evidence: dict | None = None,
) -> tuple[bool, dict]:
    """True readiness: database answers, schema is current, registry loaded.

    `storage` plus its detection evidence (volume root, db dir, mount point
    found, reason) are surfaced so operators can verify persistence from
    /ready instead of trusting the filesystem's looks (M1.3 §3)."""
    detail: dict = {"agents": agents_loaded, "storage": storage}
    if storage_evidence:
        detail["storage_evidence"] = storage_evidence
    try:
        with db.read_conn() as conn:
            conn.execute("SELECT 1").fetchone()
            version = migrations.current_version(conn)
    except Exception as exc:  # any DB failure means not ready
        detail["error"] = f"database check failed: {exc}"
        return False, detail
    detail["schema_version"] = version
    detail["schema_latest"] = migrations.latest_version()
    if version != migrations.latest_version():
        detail["error"] = "pending migrations"
        return False, detail
    if agents_loaded < 1:
        detail["error"] = "no agents loaded"
        return False, detail
    return True, detail


def environment_summary() -> dict:
    """Redacted env view for diagnostics (never values, only presence)."""
    return {
        "env": os.getenv("ATLAS_ENV", "development"),
        "access_token_set": bool(os.getenv("ATLAS_ACCESS_TOKEN")),
        "secret_key_set": bool(os.getenv("ATLAS_SECRET_KEY")),
        "db_path": os.getenv("ATLAS_DB_PATH", "(default)"),
    }
