"""Atlas HTTP layer.

Thin FastAPI wiring over the deterministic kernel. UI assets live in
atlas/static/ (M1-A): backend patches merge without touching the interface,
and a private deployment may keep its own newer UI against the same API.

Auth (M1-D): a single owner secret exchanged at /auth/login for an HttpOnly
session cookie (SameSite=Strict, Secure in production); bearer tokens keep
working for programmatic clients. Production preflight (M1-C, atlas/ops.py)
refuses to start half-secured.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import math
from typing import Annotated

from pydantic import (
    BeforeValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
)

from atlas import ops
from atlas.httpguard import (
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    validate_base_url,
)
from atlas import providers as providers_module
from atlas.providers import AnthropicProvider, FakeProvider, OpenAICompatProvider
from atlas.db import Database
from atlas.profiles import ModelProfile, ProfileError
from atlas.runtime import Runtime
from atlas.secrets import (
    ANTHROPIC_KEY_SETTING,
    OPENAI_COMPAT_KEY_SETTING,
    SecretsError,
    SecretStore,
)
from atlas.security import RateLimiter, parse_bearer, token_matches
from atlas.sessions import SessionStore

logger = logging.getLogger("atlas")
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# HTTP request models — MODULE scope on purpose (M1.3 §1): under
# `from __future__ import annotations`, models defined inside
# create_app() left route annotations as unresolvable strings and
# FastAPI registered every JSON body as a required QUERY parameter,
# breaking login, run creation, answers, and all settings writes.
# These classes hold no per-instance state, so module scope is safe;
# tests/test_http_api.py pins this with OpenAPI requestBody assertions.
# --------------------------------------------------------------------------

def _reject_bool_and_str(value: object) -> object:
    """Coercion guard (M1.3 §2): Pydantic's lax mode turns true into 1 and
    '5' into 5, which defeats strict validation before the domain layer can
    see the original type. Mutation bodies reject both up front."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid number here")
    if isinstance(value, str):
        raise ValueError("numeric strings are not accepted; send a JSON number")
    return value


def _reject_non_integer(value: object) -> object:
    """Integer fields take JSON integers ONLY (M1.4 §3): lax Pydantic also
    converts integral floats (5.0 -> 5), so floats are rejected outright,
    including 1.0."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid integer here")
    if isinstance(value, str):
        raise ValueError("numeric strings are not accepted; send a JSON integer")
    if isinstance(value, float):
        raise ValueError("floats are not accepted here; send a JSON integer")
    return value


StrictCount = Annotated[int, BeforeValidator(_reject_non_integer)]
StrictNumber = Annotated[float, BeforeValidator(_reject_bool_and_str)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(min_length=0, max_length=512)


class ProfileBody(BaseModel):
    """Strict HTTP payload for profile upserts (M1.2 §5): unknown fields,
    wrong types, booleans-as-numbers, NaN/inf, and out-of-range values are
    422s here; the domain layer re-validates independently (never 500)."""
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(default="", max_length=128)
    max_tokens: StrictCount = Field(default=1024, ge=1, le=1_000_000)
    timeout_s: StrictNumber | None = Field(default=None, gt=0, le=3_600, allow_inf_nan=False)
    max_model_calls: StrictCount | None = Field(default=None, ge=1, le=10_000)
    max_tool_calls: StrictCount | None = Field(default=None, ge=0, le=100_000)
    input_cost_per_mtok: StrictNumber | None = Field(
        default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    output_cost_per_mtok: StrictNumber | None = Field(
        default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    cost_budget_usd: StrictNumber | None = Field(
        default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    fallback: str | None = Field(default=None, max_length=64)


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8000)


class InteractionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str = Field(min_length=1, max_length=128)
    answer: StrictBool | StrictInt | StrictFloat | str | None = Field(default=None)

    @field_validator("answer")
    @classmethod
    def _answer_bounds(cls, value):
        if isinstance(value, str) and len(value) > 8000:
            raise ValueError("answer exceeds 8000 characters")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("answer must be finite")
        return value


class AnthropicKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=512)


class OpenAICompatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(default="", max_length=128)
    timeout_s: StrictNumber = Field(default=60.0, gt=0, le=600, allow_inf_nan=False)
    api_key: str | None = Field(default=None, max_length=512)


class ActiveProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=64)


def create_app(
    *,
    env: str | None = None,
    access_token: str | None = None,
    secret_key: str | None = None,
    db_path: str | None = None,
    packages_path: str | None = None,
    ack_ephemeral: bool | None = None,
    volume_root: str | None = None,
    rate_limit: int | None = None,
    providers: dict | None = None,
) -> "FastAPI":
    """Build a fully-wired Atlas app instance (M1.2 §7).

    Every parameter falls back to its environment variable. Importing THIS
    module is side-effect-free (M1.4 §7): the production instance lives in
    atlas/asgi.py (`uvicorn atlas.asgi:app`), which is also where the
    production import-refusal fires. Tests construct isolated app/database/
    environment instances side by side without module-reload hacks.

    Isolation scope: db_path/env/access_token/rate_limit/secret_key are
    fully instance-local (all settings endpoints use this SECRET_KEY).
    Runtime's BOOT-time provider construction still reads the
    ATLAS_SECRET_KEY environment variable; pass `providers=` for full
    provider isolation in tests, or set the env var."""
    DB_PATH = db_path or os.getenv("ATLAS_DB_PATH", str(ROOT / "atlas.db"))
    PACKAGES_PATH = Path(packages_path or os.getenv("ATLAS_PACKAGES_PATH", str(ROOT / "packages")))
    ATLAS_ENV = (env or os.getenv("ATLAS_ENV", "development")).strip().lower()
    IS_PROD = ATLAS_ENV == "production"
    ACCESS_TOKEN = access_token if access_token is not None else os.getenv("ATLAS_ACCESS_TOKEN", "")
    SECRET_KEY = secret_key if secret_key is not None else os.getenv("ATLAS_SECRET_KEY", "")
    ACK_EPHEMERAL = (ack_ephemeral if ack_ephemeral is not None
                     else os.getenv("ATLAS_ACK_EPHEMERAL_STORAGE", "") == "1")
    SESSION_COOKIE = "atlas_session"

    VOLUME_ROOT = volume_root or os.getenv("ATLAS_VOLUME_PATH", "/data")
    _report = ops.run_preflight(
        ATLAS_ENV, ACCESS_TOKEN, SECRET_KEY, DB_PATH,
        volume_root=VOLUME_ROOT, ack_ephemeral=ACK_EPHEMERAL,
    )
    for _warning in _report.warnings:
        logger.warning("preflight: %s", _warning)
    if _report.fatal:
        for _problem in _report.fatal:
            logger.error("preflight: %s", _problem)
        raise RuntimeError(
            "Atlas refused to start in production mode:\n- " + "\n- ".join(_report.fatal)
        )

    db = Database(DB_PATH)  # applies pending migrations
    runtime = Runtime(db, PACKAGES_PATH, providers=providers, secret_key=SECRET_KEY)
    session_store = SessionStore(db)
    write_limiter = RateLimiter(
        rate_limit if rate_limit is not None else int(os.getenv("ATLAS_RATE_LIMIT", "30")),
        60.0,
    )
    login_limiter = RateLimiter(10, 60.0)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Best-effort WAL checkpoint so a stopped container leaves a compact DB.
        # uvicorn drains in-flight requests on SIGTERM before this runs.
        try:
            with db.read_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # never block shutdown
            logger.warning("shutdown checkpoint skipped: %s", exc)


    app = FastAPI(title="Atlas", version="1.0", lifespan=lifespan)
    app.state.db = db
    app.state.runtime = runtime
    app.state.session_store = session_store
    app.state.preflight = _report
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        )
        if IS_PROD:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        path = request.url.path
        if path.startswith("/api") or path.startswith("/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response


    # ------------------------------------------------------------------- auth

    def _authed_via(request: Request) -> str | None:
        if not ACCESS_TOKEN:
            return "open"
        bearer = parse_bearer(request.headers.get("authorization", ""))
        if bearer and token_matches(bearer, ACCESS_TOKEN):
            return "bearer"
        if session_store.validate(request.cookies.get(SESSION_COOKIE)):
            return "session"
        return None


    def require_auth(request: Request) -> None:
        if _authed_via(request) is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )


    def origin_guard(request: Request) -> None:
        """CSRF backstop for cookie sessions (SameSite=Strict is the main guard):
        reject state changes whose Origin disagrees with the request host."""
        origin = request.headers.get("origin")
        if not origin:
            return  # non-browser clients
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")


    def rate_limited(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if not write_limiter.allow(client):
            raise HTTPException(status_code=429, detail="Rate limit exceeded; slow down", headers={"Retry-After": "60"})


    MUTATING = [Depends(require_auth), Depends(origin_guard), Depends(rate_limited)]
    READING = [Depends(require_auth)]


    @app.post("/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        origin_guard(request)
        client = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client):
            raise HTTPException(status_code=429, detail="Too many login attempts; wait a minute", headers={"Retry-After": "60"})
        if not ACCESS_TOKEN:
            return {"authenticated": True, "mode": "open"}
        if not token_matches(payload.secret, ACCESS_TOKEN):
            raise HTTPException(status_code=401, detail="Wrong access secret")
        token = session_store.create()
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=session_store.ttl_s,
            httponly=True,
            secure=IS_PROD,
            samesite="strict",
            path="/",
        )
        return {"authenticated": True, "mode": "secured"}


    @app.post("/auth/logout", dependencies=[Depends(require_auth)])
    def logout(request: Request, response: Response) -> dict[str, Any]:
        origin_guard(request)
        session_store.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"authenticated": False}


    @app.get("/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        return {
            "mode": "secured" if ACCESS_TOKEN else "open",
            "authenticated": _authed_via(request) is not None,
        }


    # ------------------------------------------------------------ health/ready

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness only: the process is up. Readiness lives at /ready."""
        return {"ok": True, "env": ATLAS_ENV, "agents": sorted(runtime.registry.agents)}


    @app.get("/ready")
    def ready() -> JSONResponse:
        is_ready, detail = ops.readiness(db, len(runtime.registry.agents), _report.storage, _report.storage_evidence)
        body = {"ready": is_ready, **detail, "env": ATLAS_ENV}
        return JSONResponse(status_code=200 if is_ready else 503, content=body)


    # ------------------------------------------------------------------- runs

    @app.get("/api/agents", dependencies=READING)
    def list_agents() -> list[dict[str, Any]]:
        blueprints = runtime.registry.agents.values()
        return sorted(
            (
                {
                    "id": b.id,
                    "name": b.name,
                    "description": b.description,
                    "handler": b.handler,
                }
                for b in blueprints
            ),
            key=lambda item: item["id"],
        )


    @app.post("/api/runs", dependencies=MUTATING)
    def create_run(request: RunCreate) -> dict[str, Any]:
        try:
            run_id = runtime.create_run(request.target_id, request.message)
            return run_detail(run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    @app.get("/api/runs", dependencies=READING)
    def list_runs() -> list[dict[str, Any]]:
        return db.list_runs()


    @app.get("/api/runs/{run_id}", dependencies=READING)
    def run_detail(run_id: str) -> dict[str, Any]:
        try:
            run = db.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run["events"] = db.get_events(run_id)
        run["interaction"] = db.pending_interaction(run_id)
        effective = run["context"].get("effective_agent") or {}
        # Who is actually doing the work (after delegation); target_id remains
        # the original entry target the user chose (M1.2 §1).
        run["effective_agent_id"] = effective.get("id", run["target_id"])
        return run


    @app.post("/api/runs/{run_id}/answer", dependencies=MUTATING)
    def answer_interaction(run_id: str, request: InteractionAnswer) -> dict[str, Any]:
        try:
            runtime.answer(run_id, request.interaction_id, request.answer)
            return run_detail(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


    # --------------------------------------------------------------- settings

    def _secret_status(store: SecretStore, key: str) -> dict[str, Any]:
        value = store.get(key)
        return {"configured": bool(value), "last4": value[-4:] if value else None}


    @app.get("/api/settings/providers", dependencies=READING)
    def provider_settings() -> dict[str, Any]:
        enabled = bool(SECRET_KEY)
        result: dict[str, Any] = {
            "secrets_enabled": enabled,
            "active_providers": sorted(runtime.providers),
            "anthropic": {"configured": False, "last4": None},
            "openai_compat": {
                "configured": False,
                "last4": None,
                **db.get_config("providers.openai_compat", {}),
            },
        }
        if enabled:
            store = SecretStore(db, key=SECRET_KEY)
            result["anthropic"] = _secret_status(store, ANTHROPIC_KEY_SETTING)
            result["openai_compat"].update(_secret_status(store, OPENAI_COMPAT_KEY_SETTING))
        return result


    @app.put("/api/settings/providers/anthropic", dependencies=MUTATING)
    def set_anthropic_key(payload: AnthropicKey) -> dict[str, Any]:
        try:
            SecretStore(db, key=SECRET_KEY).set(ANTHROPIC_KEY_SETTING, payload.api_key)
        except SecretsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.providers = Runtime._default_providers(db, SECRET_KEY)
        return provider_settings()


    @app.put("/api/settings/providers/openai_compat", dependencies=MUTATING)
    def set_openai_compat(payload: OpenAICompatSettings) -> dict[str, Any]:
        try:
            base_url = validate_base_url(payload.base_url, is_prod=IS_PROD)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not payload.base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
        db.set_config(
            "providers.openai_compat",
            {
                "base_url": base_url.rstrip("/"),
                "model": payload.model,
                "timeout_s": payload.timeout_s,
            },
        )
        if payload.api_key:
            try:
                SecretStore(db, key=SECRET_KEY).set(OPENAI_COMPAT_KEY_SETTING, payload.api_key)
            except SecretsError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.providers = Runtime._default_providers(db, SECRET_KEY)
        return provider_settings()


    @app.get("/api/settings/profiles", dependencies=READING)
    def list_profiles() -> dict[str, Any]:
        profiles = runtime.profiles.list()
        return {"profiles": [profiles[pid].to_dict() for pid in sorted(profiles)]}


    @app.put("/api/settings/profiles/{profile_id}", dependencies=MUTATING)
    def upsert_profile(profile_id: str, payload: ProfileBody) -> dict[str, Any]:
        data = payload.model_dump(exclude_none=True)
        data.setdefault("model", "")
        data["id"] = profile_id
        try:
            runtime.profiles.upsert(ModelProfile.from_dict(data))
        except ProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return list_profiles()


    @app.delete("/api/settings/profiles/{profile_id}", dependencies=MUTATING)
    def delete_profile(profile_id: str) -> dict[str, Any]:
        try:
            deleted = runtime.profiles.delete(profile_id)
        except ProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": deleted, **list_profiles()}


    # ------------------------------------------------------------------ shell

    @app.get("/api/settings/active-profile", dependencies=READING)
    def get_active_profile() -> dict[str, Any]:
        profile_id = runtime.profiles.active_profile_id()
        profile = runtime.profiles.get(profile_id)
        return {
            "active_profile": profile_id,
            "profile": profile.to_dict() if profile else None,
        }


    @app.put("/api/settings/active-profile", dependencies=MUTATING)
    def set_active_profile(payload: ActiveProfileBody) -> dict[str, Any]:
        try:
            profile = runtime.profiles.set_active_profile(payload.profile_id)
        except ProfileError as exc:
            status = 404 if "not found" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {"active_profile": profile.id, "profile": profile.to_dict()}


    PROVIDER_KEY_SETTINGS = {
        "anthropic": ANTHROPIC_KEY_SETTING,
        "openai_compat": OPENAI_COMPAT_KEY_SETTING,
    }


    def _provider_for_probe(name: str) -> tuple[Any, str] | dict[str, Any]:
        """Build a throwaway provider from stored config, or an error report."""
        if name == "fake":
            return FakeProvider(), ""
        if not SECRET_KEY:
            # Expected condition in open development mode: a controlled,
            # redacted config report — never a 500 (M1.3 §5).
            return {"ok": False, "provider": name, "category": "config",
                    "detail": "Encrypted provider settings are disabled "
                              "(ATLAS_SECRET_KEY is not set)"}
        store = SecretStore(db, key=SECRET_KEY)
        if name == "anthropic":
            key = store.get(ANTHROPIC_KEY_SETTING)
            if not key:
                return {"ok": False, "provider": name, "category": "config",
                        "detail": "No Anthropic API key stored"}
            return AnthropicProvider(api_key=key), ""
        config = db.get_config("providers.openai_compat", {}) or {}
        key = store.get(OPENAI_COMPAT_KEY_SETTING)
        if not key or not config.get("base_url"):
            return {"ok": False, "provider": name, "category": "config",
                    "detail": "OpenAI-compatible base_url and API key must be stored first"}
        provider = OpenAICompatProvider(
            api_key=key,
            base_url=str(config["base_url"]),
            model=str(config.get("model", "")),
            timeout=float(config.get("timeout_s", 60.0)),
        )
        return provider, str(config.get("model", ""))


    @app.post("/api/settings/providers/{name}/test", dependencies=MUTATING)
    def test_provider(name: str) -> dict[str, Any]:
        if name not in {"fake", *PROVIDER_KEY_SETTINGS}:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")
        built = _provider_for_probe(name)
        if isinstance(built, dict):
            return built  # configuration problem, reported without a network call
        provider, model = built
        return providers_module.probe(provider, model=model)


    @app.delete("/api/settings/providers/{name}/key", dependencies=MUTATING)
    def remove_provider_key(name: str) -> dict[str, Any]:
        setting = PROVIDER_KEY_SETTINGS.get(name)
        if setting is None:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")
        if not SECRET_KEY:
            raise HTTPException(status_code=409, detail="ATLAS_SECRET_KEY is not configured")
        store = SecretStore(db, key=SECRET_KEY)
        removed = store.delete(setting)
        runtime.providers = Runtime._default_providers(db, SECRET_KEY)
        return {"removed": removed, "active_providers": sorted(runtime.providers)}


    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # Added last so it wraps everything, metering the raw receive stream
    # before any body-consuming middleware or route runs (M1.3 §6).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    return app

