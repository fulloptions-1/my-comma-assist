# Verification (M1)

## 1. Kernel suite (runs anywhere)
```
pip install -r requirements.txt && python -m pytest -q
```
Expected: **73 passed** (was 51 at the S1–S7 baseline). In the authoring sandbox the
same tests ran via an offline pytest-compatible harness: `73 passed, 0 failed`.

## 2. HTTP layer (needs fastapi — could NOT be executed in the authoring sandbox)

As of M1.2 this flow is COMMITTED as automated tests: `tests/test_http_api.py`
(13 tests) builds isolated apps via `create_app` and covers everything below
plus malformed-payload, body-limit, Retry-After, storage-ack, and
effective-agent checks. `python -m pytest -q tests/` runs them wherever
fastapi is installed. Where it is not, `python scripts/check_app_import.py`
smoke-executes the module under stubbed frameworks (preflight, migrations,
route registration, factory isolation, production refusal). The script below
remains as a human-readable reference.

The M1 version of this script omitted `ATLAS_SECRET_KEY`, so the documented
provider-settings step actually returned HTTP 409 (M1.1 §10). Fixed: a valid
temporary Fernet key is generated up front, so every step below succeeds as
written.

```python
import os
from cryptography.fernet import Fernet
os.environ.update(
    ATLAS_DB_PATH="/tmp/v.db",
    ATLAS_ACCESS_TOKEN="tok-123",
    ATLAS_SECRET_KEY=Fernet.generate_key().decode(),
)
from fastapi.testclient import TestClient
from atlas.asgi import app  # atlas.app itself is import-side-effect-free (M1.4)
c = TestClient(app)

assert c.get("/health").status_code == 200
assert c.get("/ready").json()["ready"] is True
assert c.get("/api/runs").status_code == 401                       # no credentials
assert c.post("/auth/login", json={"secret": "wrong"}).status_code == 401
r = c.post("/auth/login", json={"secret": "tok-123"}); assert r.status_code == 200
assert "atlas_session" in r.cookies                                 # HttpOnly cookie set
assert c.get("/api/runs").status_code == 200                        # cookie session works
bad = c.post("/api/runs", json={"target_id": "auto", "message": "x"},
             headers={"origin": "https://evil.example"})
assert bad.status_code == 403                                       # CSRF origin backstop
run = c.post("/api/runs", json={"target_id": "assistant-agent", "message": "hi"}).json()
assert run["state"] == "COMPLETED"
assert c.post("/auth/logout").status_code == 200
assert c.get("/api/runs").status_code == 401                        # session revoked
h = {"Authorization": "Bearer tok-123"}
assert c.get("/api/runs", headers=h).status_code == 200             # bearer path intact
c.put("/api/settings/providers/openai_compat", headers=h,
      json={"base_url": "https://llm.example/v1", "model": "m", "api_key": "sk-x-123456"})
s = c.get("/api/settings/providers", headers=h).json()
assert s["openai_compat"]["last4"] == "3456" and "sk-x" not in str(s)  # redacted

# --- M1.1 additions ---------------------------------------------------------
r = c.post("/auth/login", json={"secret": "tok-123"})                # fresh session
ap = c.get("/api/settings/active-profile").json()
assert ap["active_profile"] == "fake-default"
assert c.put("/api/settings/active-profile",
             json={"profile_id": "anthropic-default"}).status_code == 200
assert c.put("/api/settings/active-profile",
             json={"profile_id": "ghost"}).status_code == 404
c.put("/api/settings/active-profile", json={"profile_id": "fake-default"})
t = c.post("/api/settings/providers/fake/test").json()
assert t["ok"] is True and t["snippet"]                              # probe works
t = c.post("/api/settings/providers/anthropic/test").json()
assert t["ok"] is False and t["category"] == "config"                # no key stored
assert c.delete("/api/settings/providers/openai_compat/key").json()["removed"] is True
resp = c.get("/api/runs")
assert resp.headers["cache-control"] == "no-store"                   # §13 headers
assert resp.headers["x-content-type-options"] == "nosniff"
for _ in range(40):                                                  # trip the limiter
    last = c.post("/api/runs", json={"target_id": "assistant-agent", "message": "hi"})
if last.status_code == 429:
    assert last.headers["retry-after"] == "60"
```

Also run the JavaScript syntax check: `node --check atlas/static/app.js`.

## 3. Production preflight
```python
import os; os.environ["ATLAS_ENV"] = "production"
# with ATLAS_ACCESS_TOKEN/ATLAS_SECRET_KEY unset:
import importlib, atlas.app   # must raise RuntimeError listing both missing variables
```
(Verified in the sandbox via framework-stubbed import: refusal message confirmed.)

## 4. Migration upgrade from a live pre-M1 database
Copy the production DB, then: `python -c "from atlas.db import Database; Database('copy.db')"`
— expect `schema_migrations` rows 1–3, all existing rows intact
(`tests/test_migrations.py` proves both upgrade paths from synthetic old schemas).

## 5. Secrets hygiene
```
git log -p | grep -iE "sk-ant|api_key.{0,4}=.{8,}" ; strings atlas.db | grep -i sk-
```
Expect no plaintext keys; session table stores only SHA-256 hashes (tested).


## 6. M1.2 additions (kernel-verifiable)
```bash
python -m pytest -q tests/                 # includes HTTP tests where fastapi exists
python scripts/check_app_import.py         # stub-framework module execution
python scripts/check_artifact_hygiene.py <tree.zip> <patches.zip>
```
Behavioral proofs now committed: immediate-restart execution recovery
(`tests/test_approval_crash.py`), delegated resume identity + drift refusal
(`tests/test_effective_agent.py`), budget boundaries incl. restart persistence
(`tests/test_active_profile.py`), atomic continuations + redaction
(`tests/test_continuation_crash.py`), durability matrix (`tests/test_ops.py`),
backup/restore (`tests/test_backup.py`).


## 7. M1.3 notes
The authoring sandbox cannot install the pinned dependencies (no network),
so the HTTP suite is REQUIRED to fail collection there (§7) — the kernel
harness reports exactly that. The reviewer-side gate is: pinned install,
`python -m pytest -q tests/` with every HTTP test collected, including
`test_openapi_mutation_routes_register_request_bodies` (the §1 regression
pin). scripts/check_app_import.py remains useful but is structural only —
it cannot detect FastAPI request-body registration problems and must never
be treated as HTTP verification.
