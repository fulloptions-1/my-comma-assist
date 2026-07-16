# FABLE5 Changelog — Implemented Changes, Verification, and Acceptance Status

Reviewer: Claude Fable 5 (Anthropic), 2026-07-15. Base: `87bc8c9` on `atlas-claude-review`.
Companion docs: `FABLE5_FINDINGS.md` (defects F-01…F-13 with reproductions), `FABLE5_IMPLEMENTATION_PLAN.md` (slice rationale, deferred contracts, rejections).

## 0. How these changes were verified (environment honesty)

The review sandbox has no PyPI/network access and no FastAPI/pytest. Therefore:

- The **deterministic kernel** (`db`, `registry`, `tools`, `runtime`, `providers`, `secrets`, `security` — stdlib + PyYAML + cryptography only) was **fully executed**: 51 tests run green under a pytest-compatible offline harness, plus end-to-end kernel smokes reproduced below. These tests are standard pytest tests; run `pytest -q` on a normal machine to confirm (expected: 51 passed).
- `atlas/app.py` is **syntax-checked and statically reviewed only** (FastAPI cannot import here). Its handlers remain thin wrappers over kernel calls that are tested. HTTP-level assertions are listed in §3 for you to run.
- Implementation landed in dependency order S1→S3→S2→S4→S5→S6→S7 (S2's runtime consumes S3's gateway API); the plan's numbering is logical, the commits note the reorder.

## 1. Changes by commit

1. **docs: findings** — baseline evidence, 13 defects with reproductions, secret scan, docs-accuracy check.
2. **docs: implementation plan** — slices S1–S7, deferred contracts, rejected complexity.
3. **fix(db)** — explicit `LEGAL_TRANSITIONS` enforced in-transaction; terminal states immutable (F-03); `expected_state` CAS guards; atomic `transition_with_interaction` (F-07); run/kind-bound single-use `resolve_interaction`; `update_context` primitive; closing `read_conn()` on all reads (F-06, measured leak eliminated); `resource_locks` + `settings` tables; same-day history tie-break (F-11). Tests: `test_state_machine.py` (7).
4. **feat(tools)** — gateway-enforced allow-lists with durable `tool.denied` (F-04/E2); typed input schemas validated before any row (E1); canonical input hashing; crashed-execution lease→ABANDONED→safe retry, exactly-once side effect (F-05/E4-E6); leased resource locks serializing conflicting writes (E8); per-tool timeouts with audit (E7/E9); oversized-result event trimming (E10 partial). Tests: `test_tool_gateway.py` (8).
5. **fix(runtime)** — `answer()` validates BEFORE resolving (F-01), verifies run binding (F-02), kind, and PENDING; atomic waits; startup `recover()`: orphaned CREATED/RUNNING → FAILED `run.recovered_orphan` (F-09), wedged WAITING runs repaired by deterministic interaction re-issue (`run.repaired`) and answerable to completion, stale tool sweep; every tool call passes the pinned blueprint allow-list. Tests: `test_recovery.py` (7).
6. **feat(registry)** — duplicate package ids, unknown handlers (against the runtime's declared set), bad versions, wrong-typed lists, unknown manifest/agent fields all rejected as one actionable batch (F-10); invalid definitions never become blueprints; per-blueprint SHA-256 `fingerprint` recorded on every run as `definition_fingerprint` (A7 partial). Tests: `test_registry_validation.py` (6).
7. **feat(providers)** — typed provider contract with normalized error categories (D1/D6/D10-errors); deterministic `FakeProvider` incl. scripted failures (D2); `AnthropicProvider` (Messages API, typed tool use, injectable HTTP transport — fully unit-tested from fixtures, D3 mocked); generic `llm` runtime handler: kernel-owned loop with budgets that fail loudly (B10 partial/D8 partial), gateway-permission-checked typed tool calls with `run:{id}:call:{n}` idempotency, durable `user.ask` waits that survive restarts, denials surfaced to the model as data while side effects stay blocked; llm agents may not declare side-effect tools yet (rejected at load); keyless `core/assistant` demo agent. Tests: `test_providers.py` (4) + `test_llm_agent.py` (7).
8. **feat(secrets)** — Fernet-encrypted `settings` under env `ATLAS_SECRET_KEY`; redacted `{configured,last4}` status; plaintext proven absent from every DB byte; wrong/missing master key fails loudly; runtime auto-wires `anthropic` only when configured (D4/D5). `requirements.txt` + `cryptography==46.0.6`. Tests: `test_secrets.py` (4).
9. **feat(security)** — pure `atlas/security.py` (bearer parse, constant-time compare, fixed-window rate limiter) + `app.py` wiring: `ATLAS_ACCESS_TOKEN`-gated `/api/*` (401 + `WWW-Authenticate`), per-IP rate limits with `Retry-After`, deliberate same-origin CORS (no middleware, documented), redacted provider-settings endpoints with live provider activation, UI token prompt/storage/re-prompt (F-08 mitigation; L1/L2 partial, L3). Tests: `test_security_unit.py` (4).
10. **test(runtime)** — tampered approval payload refused, PENDING preserved, side effect blocked (matrix F6).

Full suite after all commits: **51 passed, 0 failed** (offline harness; identical tests runnable via `pytest -q`).

Kernel smoke over the finished code (fresh DB):

```
invalid answer rejected: Enter mileage as a number in kilometres
state after bad answer: WAITING_FOR_USER | pending kept: True
car run: COMPLETED | Logged oil change at 142,300 km.
history run: COMPLETED | Latest: oil change at 142,300 km on 2026-07-15.
llm run parked: WAITING_FOR_USER | prompt: Which vehicle?
llm run after restart+answer: COMPLETED | Your last event: oil change at 142,300 km.
llm events: run.created, run.started, agent.started, agent.model_called,
  run.waiting_for_user, user_input.requested, user_input.resolved, run.resumed,
  agent.model_called, tool.started, tool.completed, agent.tools_applied,
  agent.model_called, run.completed
```

## 2. Behavior notes and compatibility

- Same-state "transitions" are now illegal; context persistence uses `update_context` (two internal call sites converted).
- Approval hashes intentionally keep the original insertion-order JSON so approvals pending **before** this upgrade remain answerable; tool-input idempotency hashes are canonical (sorted-key). New tool executions therefore hash differently from pre-upgrade ones only if input key order differed — retries of pre-upgrade keys with reordered inputs are rejected as different input (safe direction).
- Pre-existing wedged runs (answerless WAITING) are **repaired automatically** at first boot of the new code; pre-existing RUNNING zombies are failed loudly with `run.recovered_orphan`.
- `/health` and `/` stay unauthenticated (platform healthchecks / static shell); everything under `/api/*` requires the bearer token once `ATLAS_ACCESS_TOKEN` is set.
- Backup note: with the service stopped or quiesced, `sqlite3 /data/atlas.db ".backup /data/backup-$(date +%F).db"` produces a consistent copy (documented, not yet automated/tested — see L12).

## 3. Verification commands for a networked machine

```bash
git checkout atlas-claude-review
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt                        # A1
pytest -q                                              # A2  (expect: 51 passed)

# open local-dev mode
uvicorn atlas.app:app --port 8000
curl -s localhost:8000/health

# secured mode
export ATLAS_ACCESS_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(24))')
export ATLAS_SECRET_KEY=$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
uvicorn atlas.app:app --port 8000
curl -si localhost:8000/api/runs | head -1                          # HTTP/1.1 401
curl -s -H "Authorization: Bearer $ATLAS_ACCESS_TOKEN" localhost:8000/api/runs
curl -s -X POST localhost:8000/api/runs \
     -H "Authorization: Bearer $ATLAS_ACCESS_TOKEN" -H 'content-type: application/json' \
     -d '{"target_id":"car-maintenance-agent","message":"Log an oil change"}'
# answer the question / approve via the UI on your phone, or POST /api/runs/{id}/answer
curl -s -X POST localhost:8000/api/runs \
     -H "Authorization: Bearer $ATLAS_ACCESS_TOKEN" -H 'content-type: application/json' \
     -d '{"target_id":"assistant-agent","message":"say hi"}'         # keyless fake provider
for i in $(seq 1 35); do curl -s -o /dev/null -w '%{http_code} ' -X POST localhost:8000/api/runs \
     -H "Authorization: Bearer $ATLAS_ACCESS_TOKEN" -H 'content-type: application/json' \
     -d '{"message":"x"}'; done; echo                                # 429s after limit
curl -s -X PUT localhost:8000/api/settings/providers/anthropic \
     -H "Authorization: Bearer $ATLAS_ACCESS_TOKEN" -H 'content-type: application/json' \
     -d '{"api_key":"sk-ant-..."}'                                   # {configured:true,last4:...}
strings atlas.db | grep -c 'sk-ant' # 0 — key encrypted at rest

docker build -t atlas . && docker run -e ATLAS_ACCESS_TOKEN -e ATLAS_SECRET_KEY -p 8000:8000 atlas
```

Live Anthropic smoke (needs a real key; ACCOUNT-BLOCKED for the reviewer): after the PUT above, create a run for an agent whose `provider: anthropic` and confirm typed tool calls in the event log.

## 4. Acceptance matrix status

Statuses per the matrix taxonomy. "sandbox" = not executable in the offline review environment; command shipped in §3.

| ID | Status | Evidence | Remaining work | Priority |
|---|---|---|---|---|
| A1 | PARTIAL | deps pinned & consistent; install command §3 (sandbox) | run pip install once | High |
| A2 | PASS | 51/51 via pytest-equivalent harness; `pytest -q` §3 | confirm on networked machine | High |
| A3 | PASS | dup agent id (baseline test) + dup package id (`test_registry_validation`) | — | — |
| A4 | PARTIAL | unknown tools/handlers/fields/versions rejected (tests) | skills/workflows don't exist yet | Med |
| A5 | DEFERRED | no workflow engine | cycle rejection specified in plan (G) | Med |
| A6 | PARTIAL | failed `load()` never replaces last valid in-memory set (by construction) | draft/publish persistence pipeline | Med |
| A7 | PARTIAL | stable SHA-256 fingerprint recorded per run (test) | immutable publication store | Med |
| B1 | PASS | `test_state_machine` every edge + rejections | — | — |
| B2 | PASS | terminal-immutability test | — | — |
| B3 | PASS | baseline probe: 8 threads × 25 appends, contiguous, 0 errors | periodic re-stress in CI | Low |
| B4 | PARTIAL | events + projection commit atomically; ordered, complete log | rebuild-from-events routine | Low |
| B5 | PASS | restart tests (baseline + recovery + llm) | — | — |
| B6 | PASS | orphan-run recovery + tool-execution lease/abandon tests | worker/task leases when workers exist | Med |
| B7 | DEFERRED | no child runs | contract in plan | High(next) |
| B8 | PARTIAL | rejection → CANCELLED tested | workflow-policy propagation | Med |
| B9 | PARTIAL | FAILED/CANCELLED visible with badges in list/detail | dedicated dead-letter filter | Med |
| B10 | PARTIAL | llm model/tool-call budgets fail loudly (tests) | cost budgets; automation budgets | Med |
| C1 | PASS | direct runs show no resolver/concierge events (tests) | — | — |
| C2 | DEFERRED | no workflows | — | — |
| C3 | PASS | resolver test; concierge only for unresolvable | — | — |
| C4 | PARTIAL | unresolvable → explicit "could not choose" + agent list | ask-user interaction instead | Med |
| C5 | PARTIAL | delegation runs inline in the same durable run (documented) | durable child work (B7) | High(next) |
| C6 | DEFERRED | no permission/availability metadata on capabilities | needs capability ACLs | Low |
| D1 | PASS | contract + tests | — | — |
| D2 | PASS | scripted + keyless default modes tested end-to-end | — | — |
| D3 | PASS (mocked) | wire-format + tool-use + category tests from fixtures | live smoke: ACCOUNT-BLOCKED (API key) | High |
| D4 | PASS | Fernet at rest; DB-byte scan test | — | — |
| D5 | PASS | redaction tests (bytes/events/run dumps/status) | — | — |
| D6 | PASS | timeout/rate_limit/auth/malformed/transport tests | — | — |
| D7 | PASS | completed-side-effect replay after crash test (E6) | — | — |
| D8 | PARTIAL | per-call usage events; call-count budgets enforced | token/cost budget enforcement | Med |
| D9 | PASS | no legacy transport present; adapter isolation mandated (plan) | keep enforcing at review | — |
| D10 | PARTIAL | provider responses fully typed; tool inputs validated | declared output schemas for agents | Med |
| E1 | PARTIAL | input schemas validated pre-row (tests) | output validation | Med |
| E2 | PASS | denial test + live-path allow-lists | — | — |
| E3 | PASS | key required by contract + UNIQUE constraint | — | — |
| E4 | PASS | replay test | — | — |
| E5 | PASS | conflict test | — | — |
| E6 | PASS | crash-after-side-effect retry test: exactly one row | — | — |
| E7 | PASS | timeout test (FAILED + error_class) | per-tool retry counts if needed | Low |
| E8 | PASS | two-thread lock serialization test | — | — |
| E9 | PASS | tool.failed/tool.denied audit events asserted | — | — |
| E10 | PARTIAL | event trimming + full result retained; replay intact | artifact service | Med |
| E11 | PARTIAL | provider-key redaction proven; no secret-bearing tools exist | redaction hooks for future tools | Med |
| E12 | DEFERRED | — | MCP STDIO ToolSpec factory (plan) | High(next) |
| E13 | DEFERRED | — | MCP Streamable HTTP (plan) | High(next) |
| E14 | DEFERRED | single in-process adapter today | shared executor contract (plan J) | Med |
| F1 | PASS | typed kinds/payload/prompt; tests | — | — |
| F2 | PASS | restart tests (car + llm) | — | — |
| F3 | PARTIAL | any bearer-authorized client may answer (single token) | per-channel identity | Low |
| F4 | PASS | duplicate answer rejected, state intact (tests) | — | — |
| F5 | PASS | SHA-256 payload binding tests | — | — |
| F6 | PASS | tamper test: refused, PENDING kept, side effect blocked | — | — |
| F7 | PARTIAL | rejection cancels (tested) | workflow branch policy | Med |
| F8 | DEFERRED | no expiry | timer sweep + policy (plan) | Med |
| F9 | PARTIAL | answers audited as events with interaction id | responder identity/channel fields | Low |
| G1–G12 | DEFERRED | — | workflow DAG engine per plan contracts | High(next) |
| H1–H8 | DEFERRED | — | automations/schedules/webhooks per plan | High(next) |
| I1 | DEFERRED | no memory service | plan contract | Med |
| I2,I3,I6,I7 | DEFERRED | — | with artifact/memory services | Med |
| I4 | PARTIAL | content hashing exists for approval payloads | immutable artifact store | Med |
| I5 | PARTIAL | approvals bind exact payload hash (tests) | artifact-version references | Med |
| J1–J10 | DEFERRED | — | node/executor contract per plan | Med |
| K1 | PARTIAL | prior deployment demonstrated; UI unchanged in shape (sandbox) | tap through on iPhone | High |
| K2 | PARTIAL | state badges, prompts, event feed | UX pass | Low |
| K3 | PARTIAL | inline question/approval rendering (code-inspected) | browser check | High |
| K4 | DEFERRED | — | PWA manifest + service worker (plan) | Med |
| K5 | PARTIAL | one-time token prompt + localStorage + re-prompt | manual test | High |
| K6 | PARTIAL | API/poll errors now surface inline | offline states | Low |
| K7 | DEFERRED | 2s polling unchanged (F-12) | SSE per plan | Med |
| K8 | PARTIAL | contrast/touch-size reasonable; not audited | a11y audit | Low |
| K9 | PARTIAL | status badges in list; no filters | filters | Low |
| K10 | PASS | raw JSON behind `<details>`, not primary | — | — |
| K11 | DEFERRED | — | gateway adapters (plan) | Med |
| L1 | PARTIAL | bearer auth implemented; primitives unit-tested; HTTP static (sandbox) | run §3 401/200 checks | High |
| L2 | PARTIAL | limiter unit-tested; wiring static (sandbox) | run §3 429 check | High |
| L3 | PARTIAL | explicit: same-origin (no CORS middleware), CSRF-N/A rationale documented | HTTP assertion | Med |
| L4 | DEFERRED | no webhooks | with H-series | Med |
| L5 | PARTIAL | no outbound/shell/path tool surface; SQL parameterized (verified) | tests when such tools land | Med |
| L6 | PARTIAL | tool errors returned to model as data; injection stance documented | dedicated tests | Med |
| L7 | PARTIAL | idempotent additive schema: from-empty (all tests) and pre-upgrade DBs gain new tables at init | numbered migration runner | Med |
| L8 | PARTIAL | depends on Railway volume, matches docs | verify after redeploy | High |
| L9 | PARTIAL | synchronous handling + startup recovery narrow the loss window | drain-on-SIGTERM test | Low |
| L10 | PARTIAL | /health proves registry load | add DB probe | Low |
| L11 | PARTIAL | structured events + recovery stats | metrics/log shipping | Low |
| L12 | PARTIAL | backup command documented (§2) | automate + restore test | Med |
| L13 | PARTIAL | Dockerfile/railway.toml consistency verified statically (sandbox) | clean deploy | High |
| M1 | PARTIAL | legacy inventory in LEGACY_SWARM_CONTEXT.md; migration stance in plan | per-item table when migrating | Low |
| M2 | PASS | nothing transport-specific exists in kernel/adapters; boundary mandated | — | — |
| M3–M5 | DEFERRED | — | per plan | Med |
| M6 | DEFERRED | no historical logs present in this snapshot | sanitize at import time | Low |
| M7 | PARTIAL | tip-tree scan clean; branch carries the unrelated `master` fork ancestry which was out of scope | optional full-history scan | Low |

## 5. Final split (required format)

**1. Complete and tested engineering** — legal state machine w/ terminal immutability & CAS; atomic waiting+interaction; run/kind-bound single-use answers with validate-before-resolve; startup recovery incl. deterministic repair of wedged waiting runs; connection hygiene; gateway permissions/schemas/idempotency-with-lease-recovery/resource-locks/timeouts/audit-trimming; registry validation depth + definition fingerprints; provider contract + FakeProvider + AnthropicProvider (mocked transport); generic llm agent loop with durable asks, budgets, restart resume; encrypted provider settings with proven redaction; bearer-auth/rate-limit/CORS-stance primitives (pure parts) + UI token flow. 51 automated tests.

**2. Incomplete engineering (missing, with contracts in the plan)** — workflow DAG & child runs/tasks-workers (G, B7, C5), automations/schedules/webhooks (H), MCP adapters (E12/E13), artifact & memory services (I, E10 full), nodes/executors (J), gateway adapters beyond web (K11), PWA/SSE (K4/K7), migrations runner (L7), agent output schemas (D10), cost budgets (D8).

**3. Blocked only by account credentials/authorization** — pushing this commit series to GitHub (the review connector's token is read-only on this repo: both Contents and Git Data APIs return 403 for the owner's own integration — re-authorize the Claude GitHub connector with write access, or `git am` the delivered series); live Anthropic provider smoke test (needs a real API key via `PUT /api/settings/providers/anthropic`); GitHub Advanced Security secret scanning (not enabled on the repo; local pattern scan used instead); Railway redeploy/volume verification (needs the account).

**4. Intentionally deferred (reasons + acceptance criteria in the plan)** — everything in split 2, plus interaction expiry (F8), capability ACL filtering (C6), responder identity audit fields (F9).

**5. Rejected as unnecessary complexity** — message broker/queue service; Kubernetes/microservice split; vector DB for capability resolution; porting Gemini-web ghost-payload/fingerprint/tarpit rituals anywhere (if that transport ever returns it is one adapter behind the provider contract); a mandatory manager-agent hierarchy.


## M1 — Production integration release (2026-07-15)

Commits `933d3d6..HEAD` on `atlas-claude-review` (local only; account-blocked for push).
Suite: 73 passed (offline harness; `pytest -q` expected identical — see VERIFICATION.md).

- **B. Migrations**: numbered, per-migration atomic (DDL + ledger row in one
  transaction), tested from clean, original-slice, and S1–S7 databases; loud rollback.
- **D. Sessions**: owner login → HttpOnly SameSite=Strict cookie (Secure in prod),
  hashed-token store, logout, 10/min login limiter, Origin/Host CSRF backstop;
  bearer API unchanged.
- **E. Providers/profiles**: OpenAI-compatible adapter (configurable base_url/model/
  timeout, mocked contract tests); model profiles with agent>profile>default
  resolution, one-hop fallback on retryable errors, cost accounting **only** from
  user-supplied pricing with loud budget failure. No hardcoded pricing/"best model"
  claims.
- **C. Railway**: import-time preflight (prod refuses without token/key/writable DB;
  /data durability warning), `/ready` readiness vs `/health` liveness, graceful
  shutdown checkpoint, deployment/backup/rollback docs.
- **A/F. App split + UI**: backend free of embedded HTML; static chat-style reference
  UI (login, history, inline questions, exact approval payload, settings,
  diagnostics). Private newer UI must be preserved on merge (see merge guide).

Status vs acceptance matrix: matrix file lives on the remote branch (kept out of local
patches to avoid `git am` conflicts); per-item statuses tracked here. M1 items A–F:
implemented + kernel-tested; HTTP layer pending ChatGPT TestClient verification.
Honesty: nothing pushed/merged/deployed/live; provider live calls not performed.

## M1.1 — correction release for the ChatGPT M1 audit (all 14 findings)

| # | Finding | Fix | Commit |
|---|---------|-----|--------|
| 1 | Profiles not connected to the assistant | active profile in config; assistant.yaml de-hardcoded; per-run pinned snapshot; GET/PUT endpoints | `962971b`, `5784656` |
| 2 | "Hello Atlas" dead-ended in concierge | unmatched chat delegates to assistant-agent; top-score ties ask the user durably | `50900b0` |
| 3 | Tool timeout blocked past the deadline | read-only: caller returns AT deadline (no pool join); side effects: inline, cooperative contract + recorded overruns | `c6c5456` |
| 4 | Idempotency keys unbound | keys bound to run+tool; cross-reuse rejected; durable tool.replayed | `c6c5456` |
| 5 | Approved write not crash-resumable | atomic resolve+transition pins executing_approval; recovery resumes under the same key | `b54903b` |
| 6 | Session polling wrote every request | last_seen throttled to 5 min; fixed expiry documented+tested | `15568d7` |
| 7 | Profile validation gaps | numeric/length bounds; one-hop no-cycle fallbacks; guarded deletion | `c016583` |
| 8 | Fallback accounting wrong | both attempts counted; fallback profile's pricing; snapshot pinned | `962971b` |
| 9 | No provider testing | providers.probe + test/remove endpoints + UI buttons; redacted output only | `15568d7`, `5784656` |
| 10 | VERIFICATION.md §2 broken | script generates ATLAS_SECRET_KEY; M1.1 checks appended | docs commit |
| 11 | Manifest baseline SHA invalid | corrected to 87bc8c92a66323be842e82e903d8a78fc73e5a47; every SHA re-verified | manifest |
| 12 | Zip contained caches | caches purged pre-zip; scripts/check_artifact_hygiene.py gates every release | docs commit |
| 13 | Hardening batch | input bounds, security headers, no-store, Retry-After, empty/truncated responses fail loudly, env normalization, lifespan | `962971b`, `5784656` |
| 14 | Preserve the private repo's chat UI | merge guide reiterates backend-only merge; reference UI gains login/settings/test/remove/active-profile/diagnostics | `5784656`, guides |


## M1.2 — correction release for the ChatGPT M1.1 audit (all 8 findings)

| # | Finding | Fix | Commits |
|---|---------|-----|---------|
| 1 | Auto-delegated runs resumed with the wrong blueprint (empty prompt, wrong tools) | durable effective-agent snapshot pinned at start/delegation; delegation MERGES context; one verified resolution path for resume; YAML drift under a paused run fails loudly; entry target preserved, effective agent exposed to API/UI | `13fc96c` |
| 2 | Immediate restart during an approved tool execution failed the run | tool_executions.owner (migration 0004); fresh claims never stolen at claim time; boot-time reclaim of dead-process claims runs BEFORE approval resume; multi-worker path documented | `890b37c` |
| 3 | Fallback exceeded max_model_calls | helper owns attempt accounting; hop requires room for both attempts; every attempt durable (agent.provider_attempt); counts persist before failure raises; assistant.yaml budget fields removed so profile budgets actually apply | `620c956` |
| 4 | Mileage/choice/llm continuations were not atomic | resolve_and_requeue_interaction (one-tx answer→next question, direct WAITING→WAITING edge); durable context.continuation phases for delegation/llm resumed by recovery; at-least-once model calls at that boundary documented | `c095680` |
| 5 | Profile HTTP validation crashed (500) / accepted absurd values | domain: typed/finite/upper-bounded raw-data checks, never TypeError, upsert revalidates; HTTP: strict ProfileBody (extra=forbid, allow_inf_nan=False), bounded InteractionAnswer/AnthropicKey, 64KB body limit → 422/400/413, never 500 | `a4624e1`, `3af0ff2` |
| 6 | Durability detection was meaningless; backup needed a missing CLI | mount-point classification (mounted / ephemeral-acknowledged / unconfirmed); production+unconfirmed is FATAL; ATLAS_ACK_EPHEMERAL_STORAGE opt-out; /ready reports storage; Python-native scripts/backup_db.py with integrity check; docs rewritten honestly | `04c9289`, wiring corrected in `3af0ff2` |
| 7 | HTTP verification lived only in docs | create_app factory (isolated app/db/env instances, uvicorn entrypoint and prod import-refusal preserved); 13 committed TestClient tests; scripts/check_app_import.py smoke-executes the module where fastapi is absent | `3af0ff2` |
| 8 | Cleanup batch | duplicate SecretStore.delete removed; unknown wire finish reasons rejected as non-retryable malformed errors; CSP + prod HSTS; free-text answer redaction policy in events; private-repo chat UI reiterated as canonical | `7cd1edf`, `c095680`, `3af0ff2` |


## M1.3 — correction release for the ChatGPT M1.2 audit (all 9 findings)

M1.2 shipped with every JSON-body endpoint broken under the real pinned
dependencies (118/128 reviewer-side), a fact the stub-based check could not
see. M1.3's theme: make the failure classes themselves test-visible.

| # | Finding | Fix | Commits |
|---|---------|-----|---------|
| 1 | All JSON body endpoints misregistered (postponed annotations + factory-local models -> bodies became query params) | seven request models moved to module scope; `from __future__ import annotations` removed from app.py; OpenAPI requestBody/no-query-param regression pins committed | `ef1fc16` |
| 2 | true coerced to max_tokens=1; numeric strings accepted | StrictCount/StrictNumber BeforeValidators reject bool and numeric strings pre-coercion on every numeric mutation field incl. provider timeout; extra=forbid on every mutation body | `9e4b7fb` |
| 3 | Durability walk reached `/`, blessing nearly every path | explicit ATLAS_VOLUME_PATH (default /data); db must live inside it; `/` never counts; evidence {volume_root, db_dir, mounted_at, reason} in Preflight and /ready; distinct outside-root fatal | `5d925ab` |
| 4 | Factory secret key never reached Runtime provider construction | Runtime(secret_key=...); _default_providers is a pure function of (db, key); all rebuild sites use the instance key; save->activate proven env-free | `b50f3e2` |
| 5 | Provider probe without encryption key -> 500 | missing key returns a controlled redacted config report before any SecretStore construction | `b50f3e2` |
| 6 | Body limit bypassable via chunked requests | pure-ASGI BodySizeLimitMiddleware meters the receive stream (outermost); 413 with or without Content-Length; pass-through preserves parsing | `28ce04d` |
| 7 | HTTP tests silently disappeared without fastapi | required deps imported normally; missing deps fail collection loudly; local harness reports the collection error explicitly | `ef1fc16` |
| 8 | Arbitrary http:// connector URLs | validate_base_url: https in prod, loopback-only http in dev, no embedded credentials, normalized storage; policy documented | `a22ae8d` |
| 9 | Stub verification cannot see FastAPI registration | OpenAPI assertions + full TestClient suite are the committed gate; the stub check is explicitly labeled structural-only | `ef1fc16` |


## M1.4 — correction release for the ChatGPT M1.3 audit (9 findings + gate)

The decisive M1.3 lesson: the one failing test was the one that could not
run in the authoring sandbox. M1.4 moves guard logic into fastapi-free
modules so its tests execute locally too.

| # | Finding | Fix | Commit |
|---|---------|-----|--------|
| 1 | Chunked oversized bodies got FastAPI's 400, not 413 (exception raised from receive during body parse) | atlas/httpguard.py: length-less bodies are PRE-READ and metered before the app is invoked; 413 sent by the middleware; buffered messages replayed verbatim; disconnect preserved; memory bounded — unit-tested locally with fake ASGI (declared paths, exact-limit, one-over, disconnect, replay fidelity) | `fea2a65` |
| 2 | Mobile profile editor sent `id` in the body -> 422 extra_forbidden | app.js keeps the id in the URL path only; committed contract test drives the EXACT UI payload through create/update/activate/delete and pins body-with-id as 422 | `85fe15a` |
| 3 | Integer fields accepted integral floats (5.0 -> 5) | before-validator rejects every float for integer fields, incl. 1.0; domain matrix gains 5.0/2.0/1.5 | `85fe15a` |
| 4 | "Last oil change" returned the newer tire job | optional event_type filter through db -> tool -> runtime; questions naming an event filter, generic history does not; 5 interleaved-record tests | `e58c4ba` |
| 5 | Malformed usage escaped as raw ValueError | adapter-boundary normalization: usage ints (no bool/str/float/negative), non-string text, non-object tool args -> ProviderError(malformed, non-retryable) | `43b301b` |
| 6 | base_url accepted query/fragment/port 99999 | query, fragment, invalid ports (via parts.port), control chars rejected; stored value rebuilt with urlunsplit; 15-case local matrix | `fea2a65` |
| 7 | import atlas.app created atlas.db in the repo | module-level app removed; atlas/asgi.py is the uvicorn entrypoint; subprocess test proves a clean import creates nothing, runs nothing, defines no app | `90b0100` |
| 8 | NaN/inf reached tool inputs; nonstandard JSON constants parsed | gateway rejects non-finite numbers; adapters reject NaN/Infinity in bodies and tool-argument strings | `d490bbe`, `43b301b` |
| 9 | Hardening items needed honest tracking | provider response size cap FIXED (5MB, `43b301b`); the rest in docs/DEFERRED_HARDENING.md with per-item acceptance tests | `43b301b`, docs |


## M1.5 — final M1 correction for the ChatGPT M1.4 audit (4 findings)

| # | Finding | Fix | Commit |
|---|---------|-----|--------|
| 1 | Approval did not bind the event date | occurred_at pinned into context at both approval-prep sites (setdefault -> recovery reissue keeps it), included in the proposed action, shown in the prompt, bound by the artifact hash, executed verbatim (handler never regenerates a provided value); frozen-clock restart test proves the date survives midnight | `87af0b4` |
| 2 | "Last oil change" ignored oil-and-filter services | category semantics through the contract: db IN-filter (event_types), tool list parameter (JSON "array" wire type), runtime category matcher — plain-oil questions span both types, explicit oil+filter stays exact, generic history unfiltered | `39da7d4` |
| 3 | Fallback profile loaded lazily on first failure | fallback resolved, validated (exists, one-hop), and snapshotted when llm_settings are first pinned — before any provider call; edits and even deletion of the live profile cannot alter a paused run; missing fallback fails with zero provider calls | `0956bab` |
| 4 | 5MB cap applied after an unbounded read of decoded characters | _read_capped(): byte-measured, looped, bounded network reads for success and HTTPError bodies; decode only after the check; metered-stream tests prove at most cap+1 bytes are ever requested | `211ae53` |
