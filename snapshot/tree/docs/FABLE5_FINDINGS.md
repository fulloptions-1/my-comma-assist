# FABLE5 Findings — Baseline Review of `atlas-claude-review`

Reviewer: Claude Fable 5 (Anthropic), 2026-07-15.
Snapshot reviewed: commit `87bc8c9` ("Add full Atlas acceptance matrix"), branch `atlas-claude-review`.
All 15 tracked files were read in full and cross-checked against the four docs. `master` (unrelated sunnypilot fork history) was ignored as instructed.

## 1. Review environment and its limits (disclosed up front)

The review ran in a sandbox with **no outbound network for processes** (GitHub API access only). Consequences, stated so no result below over-claims:

- `pip install` from PyPI is impossible in the sandbox. Available relevant packages: Python 3.12.3, PyYAML, cryptography, requests. **Not available: fastapi, pydantic, uvicorn, pytest, httpx.**
- The deterministic kernel (`atlas/db.py`, `atlas/registry.py`, `atlas/tools.py`, `atlas/runtime.py`) imports only stdlib + yaml and was therefore **fully executed and probed**.
- The FastAPI layer (`atlas/app.py`) was **byte-compiled and statically reviewed only**; its handlers are thin 1:1 wrappers over the kernel calls that were exercised. HTTP/Docker/Railway verification commands are listed in §8 for execution on a normal machine.
- The existing test suite was executed with a pytest-compatible offline harness (module-level test functions + `tmp_path`, identical semantics for this suite). The repository's own runner remains standard pytest.
- The GitHub connector used for this review is authorized **read-only** on this repository (both Contents and Git Data write APIs return `403 Resource not accessible by integration` for the repository owner's own token). Review commits are therefore delivered as a byte-exact, `git am`-appliable patch series built on a baseline whose blob SHAs were verified identical to GitHub's.

## 2. Baseline results

```
$ python3 -m compileall -q atlas tests        # all modules compile
compile OK
$ <pytest-equivalent harness> tests/
PASS  test_vertical_slice.py::test_car_question_approval_and_idempotent_write
PASS  test_vertical_slice.py::test_history_survives_runtime_restart
PASS  test_vertical_slice.py::test_package_registry_rejects_unknown_tool
3 passed, 0 failed
```

Full car flow exercised at the kernel layer (identical code path invoked by `POST /api/runs` and `POST /api/runs/{id}/answer`):

```
direct create "Log an oil change"      -> WAITING_FOR_USER
answer "142,300"                       -> WAITING_FOR_APPROVAL
approve                                -> COMPLETED "Logged oil change at 142,300 km."
auto "When was my last oil change?"    -> COMPLETED "Latest: oil change at 142,300 km on 2026-07-15."
   events: run.created, run.started, agent.started, tool.started, tool.completed, run.completed
new Runtime over same DB file          -> earlier WAITING run still WAITING_FOR_USER, interaction still pending
```

## 3. Verified working behavior (evidence-backed)

| Behavior | Evidence |
|---|---|
| Direct target bypasses all routing (no resolver/concierge events on direct runs) | event trail above |
| Auto target resolves deterministically by intent substring; no Router agent exists anywhere | `registry.resolve()`, event trail |
| Question → approval → idempotent write happy path | test 1 + probe P1 |
| Approval binds SHA-256 of exact proposed payload; recompute-and-compare on answer | `runtime._create_approval` / `answer` |
| Repeated `car.log_maintenance` with same key returns original row; exactly one domain row | test 1 |
| WAITING run + pending interaction survive process restart | probe P1, test 2 |
| Duplicate answer to a resolved interaction rejected cleanly, state intact | probe P8 |
| Event sequence contiguous under 8 threads × 25 concurrent appends to one run (BEGIN IMMEDIATE serialization) | probe P6: 206 events, `contiguous: True`, 0 errors |
| Unknown tool / duplicate agent id / missing handler rejected at package load | `registry.load()`, test 3 |

## 4. Defects (each independently reproduced unless marked "static")

### F-01 · CRITICAL · Invalid answer permanently wedges a run
`atlas/runtime.py::answer` calls `db.resolve_interaction()` **before** validating the answer. An unparsable mileage ("banana", typed in the shipped UI) marks the interaction RESOLVED, then raises; the run stays `WAITING_FOR_USER` with **no pending interaction and no re-ask path**. Reproduced:
```
answer(run, q, "banana") -> ValueError raised
state: WAITING_FOR_USER | pending interaction: None   # STUCK = True
```
Impact: any typo dead-ends the flagship flow; no recovery scan exists to heal it (see F-09).

### F-02 · HIGH · No run↔interaction binding (cross-run confusion, second run wedged)
`answer(run_id, interaction_id, …)` never checks that the interaction belongs to `run_id`. Answering run A with run B's interaction id: A's mileage is set and A advances to approval **using A's context**, while B's interaction is consumed and B is left `WAITING_FOR_USER` with nothing pending (wedged, same end state as F-01). Reproduced (probe P3). Also lets any client resolve any run's interaction — an authorization boundary violation once auth exists.

### F-03 · HIGH · No legal state machine; terminal states resurrectable
`db.transition()` accepts any string and any source state. Reproduced: a `COMPLETED` run was transitioned back to `RUNNING` (probe P4). Violates "terminal runs cannot re-enter execution" (brief §5); nothing enforces the documented state set.

### F-04 · HIGH · Tool Gateway enforces no permissions
`ToolGateway.execute()` receives no caller identity and consults no allow-list. Reproduced: a run of `concierge-agent` (declared `tools: []`) successfully executed the side-effecting `car.log_maintenance` (probe P9). Acceptance E2 FAIL.

### F-05 · HIGH · Crash mid-tool wedges the idempotency key forever
A `tool_executions` row left `RUNNING` by a dead process blocks every retry: same input → `RuntimeError("already in progress")`; corrected input → `ValueError("…different input")`. No lease/expiry/abandon path exists. Reproduced (probe P5). The domain write itself is idempotent, so safe retry is possible in principle — the gateway just forbids it.

### F-06 · HIGH · Every read leaks a SQLite connection
All read paths (`get_run`, `list_runs`, `get_events`, `pending_interaction`, `read_maintenance`) and `init_schema` use `with self.connect() as conn:` — `sqlite3.Connection.__exit__` commits but **does not close**. Measured: 240 read calls leaked 92 file descriptors (probe P7). The shipped UI polls list+detail every 2 s ⇒ fd exhaustion of a long-running service is a matter of hours, presenting as random `OSError`/`sqlite3.OperationalError`.

### F-07 · HIGH · WAITING transition and interaction insert are separate transactions (static)
`_run_car`/`_create_approval` first commit `WAITING_*`, then insert the interaction in a second transaction. A crash between the two produces the F-01 end state (WAITING, nothing pending) with no repair path; a poller in the gap sees a waiting run with nothing to answer.

### F-08 · HIGH · Zero authentication/authorization; API and UI fully open (static)
`atlas/app.py` has no auth, no sessions, no CSRF/CORS policy, no rate limits. On the documented public Railway domain, anyone can create runs, resolve approvals, and write vehicle history. (Matches `CURRENT_ATLAS_STATE.md`; confirmed against code.) L1–L3 FAIL.

### F-09 · MEDIUM · No startup recovery scan (static)
`advance()` is invoked only inline from HTTP calls. A run left `RUNNING` by a crash stays `RUNNING` forever; nothing rescans at boot. B6 FAIL.

### F-10 · MEDIUM · Registry validation gaps (static)
Duplicate **package** ids load silently (only agent ids are deduplicated); an unknown `handler` string passes load and fails only at run time (run FAILED instead of package rejected — violates "fail loudly on invalid definitions" at the boundary); `version` is not validated as a positive integer; unknown top-level fields are silently ignored.

### F-11 · LOW · Same-day history ordering unspecified
`occurred_at` is stored as a date string; `read_maintenance` orders by it alone, so "Latest:" among same-day events is arbitrary.

### F-12 · LOW · Polling inefficiency
`GET /api/runs` returns full input/context/output for up to 50 runs every 2 s, plus a full per-run detail fetch (N+1 pattern). Works at current scale; noted for K7.

### F-13 · INFO · `auto` resolves at run-creation time
A resolvable message never executes the concierge handler: `create_run("auto", …)` resolves first and targets the winner directly. The concierge handler runs only for unresolvable text or an explicit concierge target. Behaviorally fine (and Router-free); docs under-specify this.

## 5. Documentation accuracy cross-check

`CURRENT_ATLAS_STATE.md` is **accurate** — an unusual and creditable finding. Every "implemented" claim checked out in code; every "not implemented" claim is genuinely absent (no provider runtime, no workflow engine, no MCP, no auth, no encrypted settings, no migrations, no tasks/workers/child runs). The two review questions it plants (interaction-resolution error window; transition/interaction ordering) are exactly defects F-01 and F-07 — confirmed real. No documentation claim was found asserting an unimplemented feature exists.

## 6. Security posture summary

Open API/UI (F-08); no gateway permissions (F-04); cross-run interaction resolution (F-02); no rate limiting; no CORS policy (FastAPI default: no CORS middleware — browsers same-origin only, but the API itself is unauthenticated); no secret storage exists yet (nothing to leak — and nothing to hold a provider key either); UI escapes all interpolated strings (`esc()`), embedded-XSS risk low; SQL uses parameterized statements throughout, no injection found; `transition()` composes column names from a fixed internal list (safe as written).

## 7. Secret scan (matrix A8)

GitHub Advanced Security scanning is not enabled on this repository (API: "Repository does not have GitHub Advanced Security enabled"). Fallback: local pattern scan (api keys, tokens, private-key blocks, `sk-`/`ghp_`/`AKIA`/`xox` prefixes, passwords) across all tracked code/config/package/test files: **no matches**. The four docs mention the words "secret"/"token" as prose only; read in full, they contain no credential-like strings. `.gitignore` excludes `.env` and `*.db`.

## 8. Deployment review (static; not executable in sandbox)

- `Dockerfile` and `railway.toml` are mutually consistent (`/health`, Dockerfile builder). Single-process uvicorn is correct for SQLite-WAL single-writer assumptions. Image runs as root (acceptable now; note for later). `/data` durability correctly depends on an attached Railway volume, exactly as the docs warn.
- Verification commands for a networked machine:
```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # A1
pytest -q                                # A2
uvicorn atlas.app:app --port 8000        # start
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/runs -H 'content-type: application/json' \
     -d '{"target_id":"car-maintenance-agent","message":"Log an oil change"}'
docker build -t atlas . && docker run -p 8000:8000 atlas
```

## 9. Confirmed-absent platform layers (missing engineering, not account issues)

Model provider interface & fake provider; generic (LLM) agent runtime with typed tool calls; encrypted provider-key settings; workflow engine / child runs / tasks & workers; automations, schedules, event ingestion; MCP adapters; artifact & memory services; nodes / external executors; gateways beyond web; auth & hardening; Postgres/migrations; observability & dead-letter views. (Matches docs; verified against code.)

Prioritized remediation and the implemented slice are specified in `docs/FABLE5_IMPLEMENTATION_PLAN.md`; each change and its verification is recorded in `docs/FABLE5_CHANGELOG.md`.
