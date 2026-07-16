"""Runtime provider activation uses instance-local secrets (M1.3 §4)."""
import os
from pathlib import Path

from cryptography.fernet import Fernet

from atlas.db import Database
from atlas.runtime import Runtime
from atlas.secrets import ANTHROPIC_KEY_SETTING, SecretStore

ROOT = Path(__file__).resolve().parents[1]


def _without_env_key():
    saved = os.environ.pop("ATLAS_SECRET_KEY", None)
    return saved


def _restore_env_key(saved) -> None:
    if saved is not None:
        os.environ["ATLAS_SECRET_KEY"] = saved


def test_runtime_activates_provider_from_injected_key(tmp_path: Path) -> None:
    saved = _without_env_key()
    try:
        key = Fernet.generate_key().decode()
        db = Database(str(tmp_path / "a.db"))
        SecretStore(db, key=key).set(ANTHROPIC_KEY_SETTING, "sk-ant-test-123")
        rt = Runtime(db, ROOT / "packages", secret_key=key)
        assert "anthropic" in rt.providers          # no env var involved
        assert "fake" in rt.providers
        assert os.environ.get("ATLAS_SECRET_KEY") is None
    finally:
        _restore_env_key(saved)


def test_runtime_without_any_key_has_only_keyless_providers(tmp_path: Path) -> None:
    saved = _without_env_key()
    try:
        rt = Runtime(Database(str(tmp_path / "b.db")), ROOT / "packages")
        assert set(rt.providers) == {"fake"}
    finally:
        _restore_env_key(saved)
