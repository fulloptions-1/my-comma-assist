"""Production preflight and readiness (M1-C kernel side)."""
from pathlib import Path

from cryptography.fernet import Fernet

from atlas.db import Database
from atlas.ops import readiness, run_preflight
from atlas.profiles import ProfileStore  # noqa: F401  (import sanity)


def test_production_refuses_missing_token_or_key(tmp_path: Path) -> None:
    report = run_preflight("production", "", "", str(tmp_path / "atlas.db"))
    joined = " ".join(report.fatal)
    assert "ATLAS_ACCESS_TOKEN" in joined and "ATLAS_SECRET_KEY" in joined
    # token + key + storage-unconfirmed (tmp dir is outside /data and, since
    # M1.3 §3, is no longer blessed by `/` being a mount point)
    assert report.production and len(report.fatal) == 3
    assert any("UNCONFIRMED" in m for m in report.fatal)


def test_development_only_warns(tmp_path: Path) -> None:
    report = run_preflight("development", "", "", str(tmp_path / "atlas.db"))
    assert report.fatal == []
    assert len(report.warnings) == 3  # token, key, storage (M1.3 §3)


def test_unwritable_db_path_is_fatal_and_nondata_path_warns(tmp_path: Path) -> None:
    # a regular file as a parent directory fails mkdir regardless of privileges
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    report = run_preflight("production", "t", "k", str(blocker / "sub" / "a.db"))
    assert any("not writable" in message for message in report.fatal)
    unconfirmed = run_preflight("production", "t", "k", str(tmp_path / "a.db"),
                                volume_root=str(tmp_path), is_mount=lambda p: False)
    assert any("UNCONFIRMED" in m for m in unconfirmed.fatal)
    assert unconfirmed.storage == "unconfirmed"


def test_readiness_reflects_schema_and_registry(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    ready, detail = readiness(db, agents_loaded=3)
    assert ready is True
    assert detail["schema_version"] == detail["schema_latest"]
    ready, detail = readiness(db, agents_loaded=0)
    assert ready is False and detail["error"] == "no agents loaded"


def test_openai_compat_provider_wired_from_config_and_secret(tmp_path, monkeypatch=None) -> None:
    import os
    from atlas.runtime import Runtime
    from atlas.secrets import OPENAI_COMPAT_KEY_SETTING, SecretStore

    root = Path(__file__).resolve().parents[1]
    db_path = str(tmp_path / "atlas.db")
    key = Fernet.generate_key().decode()
    old = os.environ.get("ATLAS_SECRET_KEY")
    os.environ["ATLAS_SECRET_KEY"] = key
    try:
        db = Database(db_path)
        db.set_config(
            "providers.openai_compat",
            {"base_url": "https://llm.example/v1", "model": "m-1", "timeout_s": 15},
        )
        rt = Runtime(Database(db_path), root / "packages")
        assert "openai_compat" not in rt.providers  # config alone is not enough

        SecretStore(Database(db_path), key=key).set(OPENAI_COMPAT_KEY_SETTING, "sk-oc-12345678")
        rt = Runtime(Database(db_path), root / "packages")
        provider = rt.providers["openai_compat"]
        assert provider.base_url == "https://llm.example/v1"
        assert provider.model == "m-1" and provider.timeout == 15.0
    finally:
        if old is None:
            os.environ.pop("ATLAS_SECRET_KEY", None)
        else:
            os.environ["ATLAS_SECRET_KEY"] = old


def test_storage_durability_matrix(tmp_path: Path) -> None:
    from atlas.ops import storage_durability

    root = str(tmp_path / "vol")
    db = str(Path(root) / "sub" / "a.db")

    # real expected mount: the declared volume root is a mount point
    mounted = run_preflight("production", "t", "k", db, volume_root=root,
                            is_mount=lambda p: p == root)
    assert mounted.storage == "mounted" and not mounted.fatal
    assert mounted.storage_evidence["mounted_at"] == str(Path(root).resolve())

    # ephemeral /data: NOTHING under the root is mounted; `/` being a mount
    # point (as it always is) must count for nothing
    ephemeral = storage_durability("/data/atlas.db", volume_root="/data",
                                   is_mount=lambda p: p == "/")
    assert ephemeral["state"] == "unconfirmed"
    assert ephemeral["reason"] == "volume_root_not_mounted"

    # arbitrary temporary path with the REAL is_mount: unconfirmed, because
    # it is outside the declared /data root — never blessed via `/`
    stray = storage_durability(str(tmp_path / "x.db"))
    assert stray["state"] == "unconfirmed"
    assert stray["reason"] == "database_outside_volume_root"

    # explicit acknowledgement covers any non-mounted condition, with the
    # detected reason preserved
    acked = run_preflight("production", "t", "k", db, volume_root=root,
                          ack_ephemeral=True, is_mount=lambda p: False)
    assert acked.storage == "ephemeral-acknowledged" and not acked.fatal
    assert acked.storage_evidence["reason"] == "volume_root_not_mounted"

    # database outside the configured volume root: distinct production fatal
    outside = run_preflight("production", "t", "k", str(tmp_path / "elsewhere.db"),
                            volume_root=root, is_mount=lambda p: True)
    assert outside.storage == "unconfirmed"
    assert any("OUTSIDE the configured volume root" in m for m in outside.fatal)

    # a volume root of `/` is never acceptable evidence
    rooty = storage_durability("/anything/a.db", volume_root="/",
                               is_mount=lambda p: True)
    assert rooty["state"] == "unconfirmed"
    assert rooty["reason"] == "volume_root_is_filesystem_root"

    dev = run_preflight("development", "t", "k", db, volume_root=root,
                        is_mount=lambda p: False)
    assert not dev.fatal  # development only warns
    assert any("not confirmed durable" in m for m in dev.warnings)


def test_readiness_surfaces_storage_state(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "atlas.db"))
    evidence = {"state": "mounted", "volume_root": "/data", "db_dir": "/data",
                "mounted_at": "/data", "reason": "mount_point_detected"}
    ok, detail = readiness(db, agents_loaded=3, storage="mounted",
                           storage_evidence=evidence)
    assert ok and detail["storage"] == "mounted"
    assert detail["storage_evidence"]["mounted_at"] == "/data"
