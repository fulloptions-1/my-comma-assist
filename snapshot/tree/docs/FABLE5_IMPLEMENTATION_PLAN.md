# FABLE5 Implementation Plan

Derived from `docs/FABLE5_FINDINGS.md`. Governing rule throughout: **models propose decisions; deterministic code owns state, execution, permissions, retries, scheduling, safety, recovery, budgets, approvals, and side effects.** One deployable service + SQLite stays the topology; nothing speculative is added.

## Ordering principle

Correctness and security of the existing slice first (S1–S2), then the smallest coherent platform layer that unblocks everything else (S3–S6: gateway policy, validation depth, provider boundary + generic LLM agent runtime, encrypted keys), then minimum practical HTTP security (S7). Workflows/automations/MCP/nodes are specified as next slices with contracts, not stubbed.

## S1 — Run kernel state machine + storage hygiene (`atlas/db.py`) — fixes F-03, F-06, F-07, enables F-01/F-02 fixes

- Explicit `LEGAL_TRANSITIONS` table for `CREATED / RUNNING / WAITING_FOR_USER / WAITING_FOR_APPROVAL / COMPLETED / FAILED / CANCELLED`; `transition()` validates inside the write transaction and raises `IllegalTransitionError`; terminal states admit no successors.
- Optional `expected_state=` compare-and-set guard on `transition()` so callers can detect races instead of clobbering.
- New atomic `transition_with_interaction()` — state change **and** interaction insert **and** both events in one `BEGIN IMMEDIATE` transaction (closes the F-07 crash window).
- `resolve_interaction(..., expected_run_id=)` enforces run↔interaction binding at the storage layer (defense in depth under F-02).
- All read paths and `init_schema` switch to a closing `read_conn()` context manager (F-06).
- Risks: legal-transition enforcement could break a hidden caller — mitigated by keeping event names/behavior identical and running the full suite. Acceptance: unit tests for every legal/illegal edge; terminal-immutability test; fd-stability test; crash-window test via injected fault.

## S2 — Interaction correctness + recovery (`atlas/runtime.py`) — fixes F-01, F-02, F-09; heals F-01/F-07 wedges

- `answer()` rewritten: load interaction → verify it belongs to the run and matches the run's waiting kind → **validate the answer first** (mileage parse; approval payload-hash check) → only then resolve + resume in the correct order. Invalid input returns an error and leaves the interaction PENDING.
- Startup `recover()` (called from `Runtime.__init__`, safe to run repeatedly):
  - runs found `RUNNING` at boot were in-flight during a crash (this engine advances synchronously) → transitioned to `FAILED` with `run.recovered_orphan` event; the operator sees them instead of silent zombies;
  - runs found `WAITING_*` with **no** pending interaction (the F-01/F-07 wedge class) → the interaction is **re-issued deterministically from run context** (`phase`), with a `run.repaired` event — pre-existing wedged runs heal on upgrade;
  - `tool_executions` stuck `RUNNING` longer than a lease window → marked `ABANDONED` (see S3) so retries become possible.
- Acceptance: invalid-answer keeps interaction pending; cross-run answer rejected with both runs intact; restart-recovery tests for each class above; existing 3 tests unchanged and green.

## S3 — Tool Gateway policy (`atlas/tools.py`) — fixes F-04, F-05; adds E1/E7/E8 substance

- `ToolSpec` gains: typed input schema (required/optional fields with types — validated before execution), `timeout_s`, `lock_key` template, retryability class.
- `execute()` gains `allowed_tools=` (caller's pinned blueprint allow-list); denial is durable (`tool.denied` event) — the gateway, not the handler, is the permission boundary.
- Resource locks: `resource_locks` table with lease expiry, acquired in the same serialized transaction style; `vehicle:{vehicle_id}` serializes conflicting maintenance writes across threads/processes.
- Stale-execution policy: `RUNNING` older than the lease → `ABANDONED`; retry with same key+same input re-executes safely (domain idempotency makes the side effect exactly-once); same key+different input still rejected.
- Per-tool timeout via worker thread; timeout marks execution `FAILED(timeout)` and is retryable.
- Event payload trimming: tool results larger than a cap are truncated in the *event* payload (full result stays in `tool_executions`) — prevents event-log explosions ahead of a real artifact service.
- Acceptance: permission-denial test; same-key/same-input replay after abandon; same-key/different-input rejection; lock-contention test (two threads, one vehicle); timeout test; oversized-result trimming test.

## S4 — Registry validation depth (`atlas/registry.py`) — fixes F-10, strengthens A3/A4/A7

- Reject: duplicate package ids; unknown `handler` (validated against the runtime's registered handler set, passed in); non-positive/non-int `version`; wrong-typed `intents`/`tools`; **unknown top-level fields** (fail loudly, never silently ignore).
- Per-blueprint content fingerprint (SHA-256 of canonicalized definition) recorded on every run (`definition_fingerprint`) — evidence for version pinning until immutable publication lands.
- Acceptance: one test per rejection class; fingerprint recorded and stable across restarts.

## S5 — Provider boundary + generic LLM agent runtime (`atlas/providers.py`, runtime `llm` handler) — D1, D2, D3(mocked), D10 partial, B10 partial

- Standard contract: `ProviderRequest` (messages, tool schemas, model profile, max tokens), `ProviderResult` (text, typed `ToolCall`s, finish_reason, usage), `ProviderError` with normalized categories (`timeout`, `rate_limit`, `auth`, `malformed`, `transport`).
- `FakeProvider`: deterministic scripted turns (tool calls + final text + injectable failures) — mandatory for offline CI and keyless use.
- `AnthropicProvider`: native low-cost adapter (`claude-haiku` class) speaking the Messages API with typed tool use, via an injectable HTTP transport (stdlib `urllib` default) so the request/response mapping is fully unit-testable with recorded fixtures and **no live key**. Live smoke test = ACCOUNT-BLOCKED.
- Runtime `llm` handler — the generic agent loop, deterministic code in charge:
  loop { provider.complete → typed tool calls executed through the Gateway (permission-checked, idempotency key `run:{id}:call:{n}`) → results appended } until final text; built-in `user.ask` tool creates a durable interaction and parks the run (`WAITING_FOR_USER`); message history persists in run context so the loop resumes after restart; hard budgets `max_model_calls` / `max_tool_calls` fail the run loudly when exceeded.
- New keyless demo agent `core/assistant` (handler `llm`, provider `fake`).
- Provider-specific parsing stays entirely inside adapters; the kernel sees only the standard contract. Any future legacy Gemini-web transport must be one adapter behind this same interface (nothing of it is ported now).
- Acceptance: provider contract tests (tool call, malformed, rate-limit category, usage); end-to-end fake-provider agent run incl. ask-user → restart → answer → tool → complete; budget-exhaustion test; denial of undeclared tools mid-loop.

## S6 — Encrypted provider settings (`atlas/secrets.py`) — D4, D5

- `settings` table; values encrypted with Fernet (AES128-CBC+HMAC via `cryptography`, added to requirements) under env `ATLAS_SECRET_KEY`; store/read helpers; API exposes only `{configured, last4}`; decrypted key exists only inside the provider call frame.
- Redaction tests: plaintext absent from DB file bytes, events, and API-shaped responses; missing `ATLAS_SECRET_KEY` fails loudly with a clear message.

## S7 — Minimum HTTP security (`atlas/security.py`, `atlas/app.py`) — F-08 mitigation: L1 partial, L2 partial, L3

- Env-gated bearer token (`ATLAS_ACCESS_TOKEN`): when set, all `/api/*` require `Authorization: Bearer …` (constant-time compare); UI prompts once and stores the token client-side, re-prompting on 401. No token env ⇒ open local-dev mode, loudly logged.
- Fixed-window in-memory rate limiter on mutating endpoints (pure class, unit-tested).
- Explicit CORS: same-origin only (no wildcard), documented.
- Deliberately **not** built now: user accounts, sessions, CSRF tokens (bearer-header auth is not cookie-based, so CSRF does not apply), OAuth. Single-owner product; a shared secret over HTTPS is the honest minimum.
- Constraint: `app.py` cannot be executed in the review sandbox (FastAPI unavailable) — pure logic lives in `atlas/security.py` with offline tests; wiring is minimal and mechanical; HTTP-level verification commands ship in the changelog.

## Explicitly deferred (next slices, with contracts — not stubbed now)

| Layer | Contract sketch | Acceptance when built |
|---|---|---|
| Tasks/workers + child runs | `tasks(id, run_id, parent_step, state, lease_owner, lease_expires)`; workers claim by lease; parent `WAITING_FOR_CHILDREN` woken by durable child-completion events | lease-loss recovery test; parent/child completion race test (matrix B6/B7) |
| Workflow DAG | versioned step graph (agent/tool/child/condition/timer/event/approval); readiness = all deps terminal; per-step retry/fallback policy | G1–G12 tests |
| Automations/schedules/webhooks | `automation_triggers` + due-scan loop; webhook: validate→persist→dedupe→enqueue→return | H1–H8 tests |
| MCP adapters | MCP STDIO + Streamable HTTP as `ToolSpec` factories behind the same Gateway execute contract | E12/E13 contract tests |
| Postgres + migrations | numbered migration runner; SQLite stays default; Postgres when concurrency demands | L7 from-empty and from-previous tests |
| Nodes / external executors (Codex, Claude Code, Comma, Pi) | `submit/stream/answer/cancel/status/collect` executor contract; node enrollment via one-time token | J1–J10 tests |
| PWA/SSE/mobile polish | manifest + service worker; SSE for live events replacing 2s polling | K4/K7 checks |

## Rejected as unnecessary complexity (with reasons)

Message broker / queue service (SQLite + leases suffice at this scale); Kubernetes/microservice split (one service is the requirement); vector DB for capability resolution (intent matching + future metadata search is adequate; embeddings only if measured misses justify it); porting any Gemini-web ghost-payload/fingerprint/tarpit machinery (belongs, if ever, inside one provider adapter — nothing in the kernel); a mandatory manager-agent hierarchy (workflows coordinate; managers stay optional judgment components).

## Commit series

1. docs: findings → 2. docs: plan → 3. S1 db kernel (+tests) → 4. S2 runtime answers/recovery (+tests) → 5. S3 gateway (+tests) → 6. S4 registry (+tests) → 7. S5 providers + llm handler (+tests, +assistant package) → 8. S6 secrets (+tests, +requirements) → 9. S7 security + app wiring (+tests) → 10. docs: changelog + acceptance matrix statuses + README note.
