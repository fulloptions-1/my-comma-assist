"""Automated HTTP-layer tests (M1.2 §7, hardened in M1.3 §7).

fastapi/httpx/cryptography are REQUIRED application dependencies
(requirements.txt) and are imported normally: if they are missing, this
module fails collection loudly instead of silently shrinking the suite to
green. Every test builds its own isolated app + database via create_app.
"""
import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from atlas.app import create_app

FERNET = Fernet.generate_key().decode()

def make_client(tmp_path: Path, **overrides) -> TestClient:
    params = dict(
        env="development",
        access_token="tok-123",
        secret_key=FERNET,
        db_path=str(tmp_path / "http.db"),
        rate_limit=1000,
    )
    params.update(overrides)
    app = create_app(**params)
    return TestClient(app)

def login(client: TestClient, secret: str = "tok-123"):
    return client.post("/auth/login", json={"secret": secret})

BEARER = {"Authorization": "Bearer tok-123"}

# ------------------------------------------------------------ auth

def test_login_logout_and_session_revocation(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    assert c.get("/api/runs").status_code == 401
    assert login(c, "wrong").status_code == 401
    response = login(c)
    assert response.status_code == 200
    assert c.get("/api/runs").status_code == 200
    assert c.post("/auth/logout").status_code == 200
    assert c.get("/api/runs").status_code == 401

def test_session_cookie_attributes(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    raw = login(c).headers["set-cookie"].lower()
    assert "atlas_session=" in raw
    assert "httponly" in raw
    assert "samesite=strict" in raw
    assert "path=/" in raw
    assert "secure" not in raw  # development

    prod = make_client(
        tmp_path, env="production",
        db_path=str(tmp_path / "prod.db"), ack_ephemeral=True,
    )
    prod_raw = login(prod).headers["set-cookie"].lower()
    assert "secure" in prod_raw

def test_bearer_authentication(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    assert c.get("/api/runs", headers=BEARER).status_code == 200
    assert c.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code == 401

def test_csrf_origin_rejection(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    login(c)
    bad = c.post(
        "/api/runs",
        json={"target_id": "assistant-agent", "message": "hi"},
        headers={"origin": "https://evil.example"},
    )
    assert bad.status_code == 403

# ------------------------------------------------------------ settings

def test_openai_settings_strictness(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    put = lambda body: c.put("/api/settings/providers/openai_compat",
                             headers=BEARER, json=body)
    assert put({"base_url": "https://x.example", "timeout_s": True}).status_code == 422
    assert put({"base_url": "https://x.example", "timeout_s": "60"}).status_code == 422
    assert put({"base_url": "https://x.example", "bogus": 1}).status_code == 422


def test_base_url_policy(tmp_path: Path) -> None:
    """M1.3 §8: https required in production; http only for loopback in
    development; embedded credentials rejected everywhere."""
    dev = make_client(tmp_path)
    put = lambda cl, url: cl.put("/api/settings/providers/openai_compat",
                                 headers=BEARER, json={"base_url": url, "api_key": "k"})
    assert put(dev, "https://llm.example/v1").status_code == 200
    assert put(dev, "http://localhost:8080/v1").status_code == 200      # dev loopback
    assert put(dev, "http://internal.corp/v1").status_code == 400       # dev non-loopback
    assert put(dev, "https://user:pass@llm.example/v1").status_code == 400
    assert put(dev, "ftp://llm.example").status_code == 400

    prod = make_client(tmp_path, env="production",
                       db_path=str(tmp_path / "p3.db"), ack_ephemeral=True)
    assert put(prod, "http://localhost:8080/v1").status_code == 400     # prod: https only
    assert put(prod, "https://llm.example/v1").status_code == 200


def test_provider_settings_redaction(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    r = c.put(
        "/api/settings/providers/openai_compat", headers=BEARER,
        json={"base_url": "https://llm.example/v1", "model": "m",
              "api_key": "sk-x-123456"},
    )
    assert r.status_code == 200
    body = c.get("/api/settings/providers", headers=BEARER)
    assert body.json()["openai_compat"]["last4"] == "3456"
    assert "sk-x-123456" not in body.text

def test_active_profile_endpoints(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    assert c.get("/api/settings/active-profile", headers=BEARER).json()[
        "active_profile"] == "fake-default"
    ok = c.put("/api/settings/active-profile", headers=BEARER,
               json={"profile_id": "anthropic-default"})
    assert ok.status_code == 200
    missing = c.put("/api/settings/active-profile", headers=BEARER,
                    json={"profile_id": "ghost"})
    assert missing.status_code == 404

def test_saved_key_activates_provider_without_env(tmp_path: Path, monkeypatch) -> None:
    """M1.3 §4: an isolated app must store AND activate a provider using its
    own secret_key, with no reliance on process environment variables."""
    monkeypatch.delenv("ATLAS_SECRET_KEY", raising=False)
    c = make_client(tmp_path)
    assert "anthropic" not in c.app.state.runtime.providers
    r = c.put("/api/settings/providers/anthropic", headers=BEARER,
              json={"api_key": "sk-ant-test-123"})
    assert r.status_code == 200
    assert "anthropic" in c.app.state.runtime.providers   # activated for runs
    removed = c.delete("/api/settings/providers/anthropic/key", headers=BEARER)
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert "anthropic" not in c.app.state.runtime.providers


def test_probe_without_secret_key_is_controlled(tmp_path: Path) -> None:
    """M1.3 §5: missing encryption config is an expected state — a redacted
    config report, never a 500."""
    c = make_client(tmp_path, secret_key="")
    fake = c.post("/api/settings/providers/fake/test", headers=BEARER)
    assert fake.status_code == 200 and fake.json()["ok"] is True
    for name in ("anthropic", "openai_compat"):
        r = c.post(f"/api/settings/providers/{name}/test", headers=BEARER)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": False, "provider": name, "category": "config",
                        "detail": "Encrypted provider settings are disabled "
                                  "(ATLAS_SECRET_KEY is not set)"}


def test_provider_test_and_remove(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    fake = c.post("/api/settings/providers/fake/test", headers=BEARER).json()
    assert fake["ok"] is True and fake["snippet"]
    anth = c.post("/api/settings/providers/anthropic/test", headers=BEARER).json()
    assert anth["ok"] is False and anth["category"] == "config"
    c.put("/api/settings/providers/openai_compat", headers=BEARER,
          json={"base_url": "https://llm.example/v1", "api_key": "sk-1"})
    removed = c.delete("/api/settings/providers/openai_compat/key",
                       headers=BEARER)
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert c.post("/api/settings/providers/nope/test",
                  headers=BEARER).status_code == 404

# ------------------------------------------------------------ hardening

def test_security_headers_and_no_store(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    r = c.get("/api/runs", headers=BEARER)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # dev

    prod = make_client(tmp_path, env="production",
                       db_path=str(tmp_path / "p2.db"), ack_ephemeral=True)
    pr = prod.get("/api/runs", headers=BEARER)
    assert "max-age" in pr.headers["strict-transport-security"]

def test_rate_limit_returns_retry_after(tmp_path: Path) -> None:
    c = make_client(tmp_path)  # fresh app: fresh login limiter (10/min)
    last = None
    for _ in range(11):
        last = login(c, "wrong-secret")
    assert last.status_code == 429
    assert last.headers["retry-after"] == "60"

def test_production_preflight_refusal_and_ack(tmp_path: Path) -> None:
    try:
        create_app(env="production", access_token="", secret_key="",
                   db_path=str(tmp_path / "x.db"))
    except RuntimeError as exc:
        assert "refused to start" in str(exc)
    else:
        raise AssertionError("production booted without secrets")
    # unconfirmed storage is fatal; the explicit ack permits boot
    app = create_app(env="production", access_token="t", secret_key=FERNET,
                     db_path=str(tmp_path / "y.db"), ack_ephemeral=True)
    detail = TestClient(app).get("/ready").json()
    assert detail["storage"] == "ephemeral-acknowledged"
    evidence = detail["storage_evidence"]
    assert evidence["volume_root"] == "/data"          # the declared expectation
    assert evidence["reason"] == "database_outside_volume_root"
    assert evidence["mounted_at"] is None

def test_ui_profile_payload_contract_full_lifecycle(tmp_path: Path) -> None:
    """M1.4 §2: the EXACT body shape the reference UI sends (no `id`, path
    authoritative, explicit nulls) must work for create, update, activate,
    and delete; a body WITH `id` is pinned as 422 extra_forbidden."""
    c = make_client(tmp_path)
    ui_body = {
        "provider": "fake",
        "model": "fake-1",
        "max_tokens": 1024,
        "cost_budget_usd": None,
        "input_cost_per_mtok": None,
        "output_cost_per_mtok": None,
        "fallback": None,
    }
    put = lambda body: c.put("/api/settings/profiles/ui-prof", headers=BEARER, json=body)
    assert put(ui_body).status_code == 200                       # create
    assert put({**ui_body, "model": "fake-2"}).status_code == 200  # update
    assert put({**ui_body, "id": "ui-prof"}).status_code == 422    # §2 pin

    active = c.put("/api/settings/active-profile", headers=BEARER,
                   json={"profile_id": "ui-prof"})
    assert active.status_code == 200                             # activate
    c.put("/api/settings/active-profile", headers=BEARER,
          json={"profile_id": "fake-default"})
    deleted = c.delete("/api/settings/profiles/ui-prof", headers=BEARER)
    assert deleted.status_code == 200                            # delete


def test_malformed_profile_payloads_never_500(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    put = lambda body: c.put("/api/settings/profiles/x", headers=BEARER, json=body)
    assert put({"provider": "p", "max_tokens": "x"}).status_code == 422
    assert put({"provider": "p", "max_tokens": 10 ** 100}).status_code == 422
    assert put({"provider": "p", "max_tokens": True}).status_code == 422
    assert put({"provider": "p", "max_tokens": "5"}).status_code == 422   # numeric string
    assert put({"provider": "p", "timeout_s": "3"}).status_code == 422
    assert put({"provider": "p", "max_model_calls": True}).status_code == 422
    assert put({"provider": "p", "max_tokens": 5.0}).status_code == 422     # §3
    assert put({"provider": "p", "max_tokens": 1.5}).status_code == 422
    assert put({"provider": "p", "max_model_calls": 2.0}).status_code == 422
    assert put({"provider": "p", "timeout_s": -1}).status_code == 422
    assert put({"provider": "p", "bogus": 1}).status_code == 422
    assert put({"provider": "p", "fallback": "x"}).status_code == 400  # domain: self-fallback
    assert put({"provider": "p", "max_tokens": 2048}).status_code == 200

def test_request_body_limit_and_answer_bounds(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    huge = c.post("/api/runs", headers=BEARER,
                  json={"target_id": "assistant-agent", "message": "x" * 70_000})
    assert huge.status_code == 413
    oversize_answer = c.post(
        "/api/runs/run_x/answer", headers=BEARER,
        json={"interaction_id": "int_x", "answer": "y" * 8_001},
    )
    assert oversize_answer.status_code == 422

def test_body_limit_applies_without_content_length(tmp_path: Path) -> None:
    """M1.3 §6: a chunked (length-less) body must not bypass the 64KB cap,
    and small chunked bodies must still parse normally afterwards."""
    c = make_client(tmp_path)

    def oversized():
        for _ in range(8):
            yield b"x" * 10_000  # 80KB total, no Content-Length

    r = c.post("/api/runs",
               headers={**BEARER, "content-type": "application/json"},
               content=oversized())
    assert r.status_code == 413

    def small():
        yield b'{"secret": "tok-123"}'

    ok = c.post("/auth/login",
                headers={"content-type": "application/json"},
                content=small())
    assert ok.status_code == 200  # stream pass-through preserved parsing


def test_run_flow_reports_effective_agent(tmp_path: Path) -> None:
    c = make_client(tmp_path)
    run = c.post("/api/runs", headers=BEARER,
                 json={"target_id": "auto", "message": "Hello Atlas"}).json()
    assert run["state"] == "COMPLETED"
    assert run["target_id"] == "concierge-agent"
    assert run["effective_agent_id"] == "assistant-agent"

# ------------------------------------------------------------ §1 regression pin

MUTATION_ROUTES = [
    ("/auth/login", "post"),
    ("/api/runs", "post"),
    ("/api/runs/{run_id}/answer", "post"),
    ("/api/settings/providers/anthropic", "put"),
    ("/api/settings/providers/openai_compat", "put"),
    ("/api/settings/profiles/{profile_id}", "put"),
    ("/api/settings/active-profile", "put"),
]


def test_openapi_mutation_routes_register_request_bodies(tmp_path: Path) -> None:
    """M1.3 §1: the postponed-annotations bug turned every JSON body into a
    required query parameter. Pin the contract at the OpenAPI level so any
    future annotation-resolution regression fails this suite immediately."""
    app = create_app(env="development", access_token="tok-123",
                     secret_key=FERNET, db_path=str(tmp_path / "openapi.db"))
    spec = app.openapi()
    for path, method in MUTATION_ROUTES:
        operation = spec["paths"][path][method]
        assert "requestBody" in operation, f"{method.upper()} {path} lost its request body"
        query_params = {
            p["name"] for p in operation.get("parameters", []) if p.get("in") == "query"
        }
        assert not ({"payload", "request", "body"} & query_params), (
            f"{method.upper()} {path} exposes body as query params: {query_params}"
        )


def test_importing_atlas_app_has_no_side_effects(tmp_path: Path) -> None:
    """M1.4 §7: `import atlas.app` in a clean interpreter must not create
    files, open the database, run migrations, run preflight, or build an
    app instance. The production instance lives in atlas.asgi."""
    import atlas

    repo_root = Path(atlas.__file__).resolve().parent.parent
    program = (
        "import atlas.app as m; "
        "assert not hasattr(m, 'app'), 'module-level app exists'; "
        "print('import-clean')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "import-clean" in proc.stdout
    assert "preflight" not in proc.stderr.lower()
    assert list(tmp_path.iterdir()) == []              # nothing created in cwd
    assert not (repo_root / "atlas.db").exists()       # nothing in the repo


def test_asgi_entrypoint_builds_the_app(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_ENV", "development")
    monkeypatch.setenv("ATLAS_DB_PATH", str(tmp_path / "asgi.db"))
    monkeypatch.setenv("ATLAS_ACCESS_TOKEN", "tok-123")
    monkeypatch.setenv("ATLAS_SECRET_KEY", FERNET)
    import importlib

    import atlas.asgi as asgi_module

    module = importlib.reload(asgi_module)  # honor this test's env
    client = TestClient(module.app)
    assert client.get("/health").status_code == 200
