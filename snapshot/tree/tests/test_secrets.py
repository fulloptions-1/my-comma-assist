"""Encrypted settings and key redaction (matrix D4/D5)."""
import os
from pathlib import Path

from cryptography.fernet import Fernet

from atlas.db import Database, dumps
from atlas.runtime import Runtime
from atlas.secrets import ANTHROPIC_KEY_SETTING, SecretsError, SecretStore

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-ant-test-EXTREMELY-SECRET-1a2b3c4d"


def make_store(tmp_path: Path) -> tuple[Database, SecretStore, str]:
    db = Database(str(tmp_path / "atlas.db"))
    key = Fernet.generate_key().decode()
    return db, SecretStore(db, key=key), key


def test_roundtrip_and_redacted_status(tmp_path: Path) -> None:
    _, store, _ = make_store(tmp_path)
    assert store.status(ANTHROPIC_KEY_SETTING) == {"configured": False, "last4": None}
    store.set(ANTHROPIC_KEY_SETTING, SECRET)
    assert store.get(ANTHROPIC_KEY_SETTING) == SECRET
    status = store.status(ANTHROPIC_KEY_SETTING)
    assert status == {"configured": True, "last4": "3c4d"}
    assert SECRET not in dumps(status)
    assert store.delete(ANTHROPIC_KEY_SETTING) is True
    assert store.get(ANTHROPIC_KEY_SETTING) is None


def test_plaintext_absent_from_database_bytes(tmp_path: Path) -> None:
    db, store, _ = make_store(tmp_path)
    store.set(ANTHROPIC_KEY_SETTING, SECRET)
    # force WAL contents into the main file, then scan every byte on disk
    with db.read_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b"".join(
        p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("atlas.db")
    )
    assert SECRET.encode() not in blob
    assert b"EXTREMELY-SECRET" not in blob


def test_wrong_and_missing_master_key_fail_loudly(tmp_path: Path) -> None:
    db, store, _ = make_store(tmp_path)
    store.set(ANTHROPIC_KEY_SETTING, SECRET)

    other = SecretStore(db, key=Fernet.generate_key().decode())
    try:
        other.get(ANTHROPIC_KEY_SETTING)
    except SecretsError as exc:
        assert "ATLAS_SECRET_KEY" in str(exc)
    else:
        raise AssertionError("wrong master key decrypted the secret")

    for bad in ("", "not-a-fernet-key"):
        try:
            SecretStore(db, key=bad)
        except SecretsError as exc:
            assert "ATLAS_SECRET_KEY" in str(exc)
        else:
            raise AssertionError(f"invalid master key accepted: {bad!r}")


def test_runtime_wires_anthropic_only_when_configured(tmp_path: Path) -> None:
    db_path = str(tmp_path / "atlas.db")
    key = Fernet.generate_key().decode()

    old = os.environ.pop("ATLAS_SECRET_KEY", None)
    try:
        rt = Runtime(Database(db_path), ROOT / "packages")
        assert set(rt.providers) == {"fake"}  # keyless mode

        os.environ["ATLAS_SECRET_KEY"] = key
        rt = Runtime(Database(db_path), ROOT / "packages")
        assert set(rt.providers) == {"fake"}  # env set, no stored key yet

        SecretStore(Database(db_path), key=key).set(ANTHROPIC_KEY_SETTING, SECRET)
        rt = Runtime(Database(db_path), ROOT / "packages")
        assert set(rt.providers) == {"fake", "anthropic"}
        assert rt.providers["anthropic"].api_key == SECRET
        # the decrypted key lives only in the adapter; nothing durable holds it
        run_id = rt.create_run("assistant-agent", "hello")  # fake provider run
        run = rt.db.get_run(run_id)
        assert SECRET not in dumps(run)
        assert SECRET not in dumps(rt.db.get_events(run_id))
    finally:
        if old is None:
            os.environ.pop("ATLAS_SECRET_KEY", None)
        else:
            os.environ["ATLAS_SECRET_KEY"] = old


def test_delete_removes_key_and_reports(tmp_path):
    import os
    from cryptography.fernet import Fernet
    from atlas.db import Database
    from atlas.secrets import SecretStore

    key = Fernet.generate_key().decode()
    store = SecretStore(Database(str(tmp_path / "s.db")), key=key)
    store.set("providers.anthropic.api_key", "sk-ant-test-123456")
    assert store.get("providers.anthropic.api_key")
    assert store.delete("providers.anthropic.api_key") is True
    assert store.get("providers.anthropic.api_key") is None
    assert store.delete("providers.anthropic.api_key") is False
