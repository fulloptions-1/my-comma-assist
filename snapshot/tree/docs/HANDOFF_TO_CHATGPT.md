# Handoff to ChatGPT (integration reviewer / deployment operator)

Ground truth: everything below is **committed locally** in Claude's sandbox and
**packaged** — nothing is pushed, merged, deployed, or live. GitHub connector writes
remain account-blocked. The FastAPI layer is syntax- and stub-import-checked only
(fastapi is not installable offline); §2 of docs/VERIFICATION.md is yours to run.

## Checkpoint log
| Milestone | Head | Artifacts | Suite |
|---|---|---|---|
| S1–S7 baseline (accepted) | `04e5082` | atlas-fable5-{patches,tree}.zip | 51 passed |
| **M1 production integration** | see manifest | atlas-v1-M1-{tree,patches}.zip + manifest | **73 passed** (offline harness; `pytest -q` expected identical) |

## M1: what to inspect
1. `atlas/migrations.py` + `tests/test_migrations.py` — atomicity claim (BEGIN…version
   row…COMMIT per migration; rollback test).
2. `atlas/app.py` — auth surface: `_authed_via`, `origin_guard`, login limiter, cookie
   flags (`httponly`, `samesite=strict`, `secure=IS_PROD`).
3. `atlas/sessions.py` — only token hashes stored (byte-scan test).
4. `atlas/profiles.py` + runtime `_complete_with_fallback` — one-hop fallback, budgets
   only from user-supplied pricing, no hardcoded prices.
5. `atlas/ops.py` — production refusal conditions.

## What to merge / what not to overwrite
Follow docs/PRODUCTION_MERGE_GUIDE.md: merge `atlas/*.py`, tests, docs, Dockerfile,
railway.toml. **Do not overwrite your newer private chat UI** with `atlas/static/` —
it is a reference implementation of the endpoint contract listed in the guide.

## Live checks after deploy (in order)
1. Deploy logs show no preflight fatals; `/ready` → 200 with `schema_version == schema_latest`.
2. Fresh phone browser: login screen → wrong secret rejected → correct secret sets
   cookie → history loads → send to `assistant-agent` completes.
3. `car-maintenance-agent` odometer question → inline answer → approval shows exact
   `proposed_action` JSON → approve → COMPLETED; reject path on a second run.
4. Settings: save a provider key → status shows `…last4` only; `strings /data/atlas.db`
   on a backup copy shows no plaintext key.
5. Restart the service mid-`WAITING_FOR_USER` run → run survives, answer still works
   (startup recovery + durable sessions).
6. Bearer `curl` still works; cross-origin POST with a forged Origin header → 403.

## Known limitations (M1)
- HTTP layer not executed in the authoring sandbox (framework unavailable offline).
- OpenAI-compatible + Anthropic adapters are contract-tested against fixtures; live
  provider calls need real keys — account-blocked here.
- UI polls (2.5s); SSE is scheduled for the PWA milestone. Legacy agent/skill/MCP
  content is NOT migrated (sources were never provided); importer lands in a later
  milestone with an explicit report format.


## M1.1 correction release (this checkpoint)
All 14 audit findings addressed; per-finding commit map in
docs/FABLE5_CHANGELOG.md. Inspect first:
- `atlas/runtime.py` — active-profile pinning, crash-resumable approvals,
  concierge->assistant routing, fallback accounting
- `atlas/tools.py` — deadline-honoring timeouts, run/tool-bound idempotency
- `atlas/db.py` — resolve_interaction_and_transition (single-transaction answer+move)
- `tests/test_approval_crash.py`, `tests/test_active_profile.py`,
  `tests/test_concierge_routing.py` — the new behavioral proofs
- `scripts/check_artifact_hygiene.py` — run it against both zips
- docs/VERIFICATION.md §2 now generates ATLAS_SECRET_KEY (the M1 script 409'd)
Manifest baseline corrected to 87bc8c92a66323be842e82e903d8a78fc73e5a47.


## M1.2 correction release (this checkpoint)
All 8 M1.1-audit findings addressed; per-finding commit map in
docs/FABLE5_CHANGELOG.md. Inspect first:
- `atlas/runtime.py` — effective-agent pinning/drift refusal, budget-reserved
  fallback with durable attempts, continuation phases + recovery dispatch
- `atlas/tools.py` + migration 0004 — owned claims, boot-time reclaim ordering
- `atlas/db.py` — resolve_and_requeue_interaction, redaction policy
- `atlas/app.py` — create_app factory (routes as closures)
- `atlas/ops.py` + `scripts/backup_db.py` — mount-based durability, backup
- `tests/test_http_api.py` — run under real pytest (skipped in the sandbox)
- `tests/test_approval_crash.py` — note the immediate-restart test now uses
  created_at=NOW with a dead owner, per your reproduction
Run: full pytest, compileall, node --check, both scripts, hygiene on both zips,
patches against the M1.1 tree (base 5055410).


## M1.3 correction release (this checkpoint)
All 9 M1.2-audit findings addressed; map in docs/FABLE5_CHANGELOG.md.
Patch base: 22117626bb0734dbbb4acdaa524501369f2a190d (M1.2 head).
Inspect first: atlas/app.py (module-scope models, no future-import,
BodySizeLimitMiddleware, validate_base_url, secret plumbing),
atlas/ops.py (volume-root durability + evidence), atlas/runtime.py
(secret_key), tests/test_http_api.py (imports REQUIRED deps normally;
includes the OpenAPI requestBody pins).
Reviewer run-list: pinned install from requirements.txt; pytest -q (every
HTTP test must COLLECT and pass); compileall; node --check; hygiene on
both zips; patches vs M1.2.


## M1.4 correction release (this checkpoint)
Patch base: 4e6d8d8f15fb309ee62b66906b0dad0e73e9bb66 (M1.3 head).
All 9 findings addressed; map in docs/FABLE5_CHANGELOG.md; deferred items
now live in docs/DEFERRED_HARDENING.md with acceptance tests.
Inspect first: atlas/httpguard.py (+ tests/test_httpguard.py — the §1 fix
is unit-tested LOCALLY now), atlas/asgi.py + Dockerfile CMD,
atlas/providers.py parse boundaries, tests/test_car_history_filter.py,
tests/test_http_api.py (UI payload contract, subprocess import test).
Entry point changed: uvicorn atlas.asgi:app.


## M1.5 final M1 correction (this checkpoint)
Patch base: 56f822ddd380808ae3fa11985ecc77aba9c4c7e6 (M1.4 head).
Four focused fixes; map in docs/FABLE5_CHANGELOG.md. The API contract is
unchanged except additively (approval payloads now include occurred_at;
car.read_history gains an optional event_types list; run detail already
exposed effective_agent_id). Nothing here touches or replaces the private
chat-style UI — integration remains yours, per protocol.
Inspect first: tests/test_approval_date.py, tests/test_car_history_filter.py,
the fallback-pinning additions in tests/test_active_profile.py, and the
metered-stream reader tests at the end of tests/test_providers.py.
