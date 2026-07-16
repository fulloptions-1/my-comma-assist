"""Model profiles.

A profile names a (provider, model) selection plus limits: max output
tokens, per-call timeout, call budgets, optional user-supplied pricing and
a cost budget, and an optional single-hop fallback profile. Agents reference
a profile by id; explicit agent fields override profile values.

No pricing is hardcoded: cost accounting activates only when the user
supplies input/output cost per million tokens, and it uses the usage numbers
the provider actually returned. Profiles are non-secret and live in the
plain `config` table; provider API keys stay encrypted in `settings`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
from typing import Any

from atlas.db import Database, dumps, loads, utc_now

CONFIG_KEY = "model_profiles"
ACTIVE_PROFILE_KEY = "assistant.active_profile"
DEFAULT_ACTIVE_PROFILE = "fake-default"


def _check_int(
    data: dict[str, Any], name: str, *, minimum: int, maximum: int
) -> None:
    value = data.get(name)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{name} must be an integer, got {type(value).__name__}")
    if not (minimum <= value <= maximum):
        raise ProfileError(f"{name} must be between {minimum} and {maximum}")


def _check_number(
    data: dict[str, Any],
    name: str,
    *,
    minimum: float,
    maximum: float,
    exclusive_min: bool = False,
) -> None:
    value = data.get(name)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ProfileError(f"{name} must be finite (got {value!r})")
    if exclusive_min:
        if not (minimum < value <= maximum):
            raise ProfileError(f"{name} must be > {minimum} and <= {maximum}")
    elif not (minimum <= value <= maximum):
        raise ProfileError(f"{name} must be between {minimum} and {maximum}")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    id: str
    provider: str
    model: str = ""
    max_tokens: int = 1024
    timeout_s: float | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    cost_budget_usd: float | None = None
    fallback: str | None = None  # id of another profile; one hop only

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ModelProfile":
        known = {f.name for f in fields(ModelProfile)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ProfileError(f"unknown profile fields: {', '.join(unknown)}")
        if not str(data.get("id", "")).strip():
            raise ProfileError("profile id is required")
        if not str(data.get("provider", "")).strip():
            raise ProfileError("profile provider is required")
        # Typed, finite, bounded validation of the RAW data (M1.2 §5): wrong
        # types, booleans-as-numbers, NaN/inf, and absurd magnitudes are all
        # domain errors — never TypeError, never silently accepted.
        _check_int(data, "max_tokens", minimum=1, maximum=1_000_000)
        _check_number(data, "timeout_s", minimum=0, maximum=3_600, exclusive_min=True)
        _check_int(data, "max_model_calls", minimum=1, maximum=10_000)
        _check_int(data, "max_tool_calls", minimum=0, maximum=100_000)
        _check_number(data, "input_cost_per_mtok", minimum=0, maximum=1_000_000)
        _check_number(data, "output_cost_per_mtok", minimum=0, maximum=1_000_000)
        _check_number(data, "cost_budget_usd", minimum=0, maximum=1_000_000,
                      exclusive_min=True)
        for name, limit in (("id", 64), ("provider", 64), ("model", 128), ("fallback", 64)):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise ProfileError(f"{name} must be a string")
            if value is not None and len(value) > limit:
                raise ProfileError(f"{name} exceeds {limit} characters")
        try:
            profile = ModelProfile(**data)
        except TypeError as exc:  # belt and braces: never leak TypeError
            raise ProfileError(f"invalid profile payload: {exc}") from exc
        if profile.fallback == profile.id:
            raise ProfileError("profile cannot fall back to itself")
        return profile

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_PROFILES: dict[str, ModelProfile] = {
    "fake-default": ModelProfile(id="fake-default", provider="fake", model="fake-1"),
    "anthropic-default": ModelProfile(
        id="anthropic-default", provider="anthropic", model="claude-haiku-4-5"
    ),
}


class ProfileStore:
    """User profiles persisted as one JSON document in `config`; defaults are
    code-defined and can be shadowed (same id) but not deleted."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _user_profiles(self) -> dict[str, ModelProfile]:
        with self.db.read_conn() as conn:
            row = conn.execute(
                "SELECT value_json FROM config WHERE key = ?", (CONFIG_KEY,)
            ).fetchone()
        if row is None:
            return {}
        return {
            pid: ModelProfile.from_dict(raw)
            for pid, raw in loads(row["value_json"], {}).items()
        }

    def _save(self, user: dict[str, ModelProfile]) -> None:
        payload = dumps({pid: p.to_dict() for pid, p in user.items()})
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO config (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
                (CONFIG_KEY, payload, utc_now()),
            )

    def list(self) -> dict[str, ModelProfile]:
        merged = dict(DEFAULT_PROFILES)
        merged.update(self._user_profiles())
        return merged

    def get(self, profile_id: str) -> ModelProfile | None:
        return self.list().get(profile_id)

    def upsert(self, profile: ModelProfile) -> ModelProfile:
        # Re-validate through from_dict so directly-constructed instances
        # get the same typed/finite/bounded checks as API payloads (M1.2 §5).
        profile = ModelProfile.from_dict(profile.to_dict())
        if profile.fallback is not None:
            target = self.list().get(profile.fallback)
            if target is None:
                raise ProfileError(f"fallback profile not found: {profile.fallback}")
            if target.fallback is not None:
                raise ProfileError(
                    "fallback chains are not allowed (one hop only): "
                    f"{profile.fallback!r} already falls back to {target.fallback!r}"
                )
        user = self._user_profiles()
        # Refuse edits that would turn an existing fallback TARGET into a chain.
        if profile.fallback is not None:
            for other in self.list().values():
                if other.fallback == profile.id and other.id != profile.id:
                    raise ProfileError(
                        f"{other.id!r} falls back to this profile; adding a further "
                        "fallback would create a chain (one hop only)"
                    )
        user[profile.id] = profile
        self._save(user)
        return profile

    def active_profile_id(self) -> str:
        return str(self.db.get_config(ACTIVE_PROFILE_KEY, DEFAULT_ACTIVE_PROFILE))

    def set_active_profile(self, profile_id: str) -> ModelProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise ProfileError(f"profile not found: {profile_id}")
        self.db.set_config(ACTIVE_PROFILE_KEY, profile_id)
        return profile

    def delete(self, profile_id: str) -> bool:
        if profile_id == self.active_profile_id():
            raise ProfileError(
                f"{profile_id!r} is the active assistant profile; switch first"
            )
        referrers = sorted(
            p.id for p in self.list().values() if p.fallback == profile_id
        )
        if referrers:
            raise ProfileError(
                f"{profile_id!r} is the fallback of: {', '.join(referrers)}"
            )
        user = self._user_profiles()
        if profile_id in user:
            del user[profile_id]
            self._save(user)
            return True
        if profile_id in DEFAULT_PROFILES:
            raise ProfileError(f"built-in profile cannot be deleted: {profile_id}")
        return False
