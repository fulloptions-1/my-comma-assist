"""Encrypted settings (provider API keys).

Values are encrypted at rest with Fernet (AES128-CBC + HMAC) under a key
supplied via the ATLAS_SECRET_KEY environment variable — never stored in the
database. Reads outside the provider call frame get only redacted status
({configured, last4}); the decrypted value must never appear in run context,
events, tool results, or API responses.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from atlas.db import Database, utc_now


GENERATE_HINT = (
    "generate one with: python -c "
    '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


class SecretsError(RuntimeError):
    pass


class SecretStore:
    def __init__(self, db: Database, key: str | None = None) -> None:
        raw = key if key is not None else os.environ.get("ATLAS_SECRET_KEY", "")
        if not raw:
            raise SecretsError(f"ATLAS_SECRET_KEY is not set; {GENERATE_HINT}")
        try:
            self.fernet = Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
        except Exception as exc:
            raise SecretsError(
                f"ATLAS_SECRET_KEY is not a valid Fernet key ({exc}); {GENERATE_HINT}"
            ) from exc
        self.db = db

    def set(self, key: str, value: str) -> None:
        if not value:
            raise SecretsError("Refusing to store an empty secret")
        token = self.fernet.encrypt(value.encode("utf-8"))
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO settings (key, value_encrypted, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value_encrypted = excluded.value_encrypted,
                    updated_at = excluded.updated_at""",
                (key, token, utc_now()),
            )

    def get(self, key: str) -> str | None:
        with self.db.read_conn() as conn:
            row = conn.execute(
                "SELECT value_encrypted FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return self.fernet.decrypt(bytes(row["value_encrypted"])).decode("utf-8")
        except InvalidToken as exc:
            raise SecretsError(
                f"Setting {key!r} cannot be decrypted with the current "
                "ATLAS_SECRET_KEY (key changed?)"
            ) from exc

    def delete(self, key: str) -> bool:
        """Remove a stored secret. Returns True when a row was deleted."""
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0

    def status(self, key: str) -> dict[str, object]:
        """Redacted view for APIs/UIs: never returns the value itself."""
        value = self.get(key)
        if value is None:
            return {"configured": False, "last4": None}
        return {"configured": True, "last4": value[-4:]}


ANTHROPIC_KEY_SETTING = "providers.anthropic.api_key"
OPENAI_COMPAT_KEY_SETTING = "providers.openai_compat.api_key"
