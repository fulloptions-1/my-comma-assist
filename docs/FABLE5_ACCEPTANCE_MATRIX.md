# Claude Fable 5 Acceptance Matrix

Use this matrix to distinguish implemented, tested, partially implemented, account-blocked, and deferred capabilities. Never mark a capability complete based only on documentation or UI text.

Status values:

- **PASS** — implemented and verified by automated test or reproducible command.
- **PARTIAL** — some behavior exists but the contract is incomplete.
- **FAIL** — implementation is missing or violates the guarantee.
- **ACCOUNT-BLOCKED** — engineering contract exists and only external authorization is missing.
- **DEFERRED** — intentionally postponed with written reason and acceptance criteria.

## A. Repository and package integrity

| ID | Requirement | Evidence required |
|---|---|---|
| A1 | Clean checkout installs successfully | exact commands and output |
| A2 | All tests pass | test count and output |
| A3 | Duplicate IDs are rejected | automated test |
| A4 | Missing tools, skills, agents, workflows, and versions are rejected | automated tests |
| A5 | Workflow cycles are rejected | automated test |
| A6 | Invalid package save rolls back without damaging the last valid version | integration test |
| A7 | Published definitions are immutable and runs pin versions | restart/version test |
| A8 | No secrets exist in tracked files or test fixtures | secret scan |

## B. Run kernel and durability

| ID | Requirement | Evidence required |
|---|---|---|
| B1 | Legal state transitions are explicit and enforced | unit tests |
| B2 | Terminal states cannot resume | unit test |
| B3 | Event sequence allocation is safe under concurrency | race/stress test |
| B4 | Current projections can be rebuilt from events | rebuild test |
| B5 | A waiting run survives process restart | end-to-end test |
| B6 | A running task abandoned by a crashed worker is recovered safely | lease/recovery test |
| B7 | Parent/child completion races cannot lose wakeups | repeated race test |
| B8 | Cancellation propagates according to policy | workflow test |
| B9 | Failed/stuck runs appear in an operator/dead-letter view | UI/API demonstration |
| B10 | Run and automation budgets stop loops and overspend | automated tests |

## C. Direct invocation and Concierge

| ID | Requirement | Evidence required |
|---|---|---|
| C1 | Explicit agent target bypasses routing/model classification | event assertion |
| C2 | Explicit workflow target bypasses Concierge | event assertion |
| C3 | Unknown request uses Capability Resolver | test |
| C4 | Low-confidence resolution asks the user rather than guessing | interaction test |
| C5 | Concierge launches durable child work and remains available | concurrency test |
| C6 | Capability search filters by permissions and availability | tests |

## D. Agent runtime and providers

| ID | Requirement | Evidence required |
|---|---|---|
| D1 | Standard Model Provider interface exists | code/contract tests |
| D2 | Deterministic fake provider supports offline CI | end-to-end test |
| D3 | At least one native low-cost provider works with typed tool calls | provider test with mocked HTTP plus optional live smoke test |
| D4 | Provider keys are encrypted at rest | database inspection test |
| D5 | Provider keys never enter events, logs, prompts, or API responses | redaction tests |
| D6 | Provider timeout/rate limit/malformed response categories are normalized | contract tests |
| D7 | Provider failure cannot repeat a completed side effect | crash/retry test |
| D8 | Token/cost usage is recorded and enforced against budgets | tests |
| D9 | Legacy Gemini web transport, if retained, is isolated in one adapter | architecture inspection |
| D10 | Agent final output is validated against its declared schema | tests |

## E. Tool Gateway

| ID | Requirement | Evidence required |
|---|---|---|
| E1 | Tool inputs and outputs are schema validated | tests |
| E2 | Agent/workflow permissions are enforced by the gateway | denial test |
| E3 | Side-effect tools require idempotency keys | test |
| E4 | Same key + same input returns original result | test |
| E5 | Same key + different input is rejected | test |
| E6 | Process death after side effect does not duplicate action | recovery test |
| E7 | Per-tool timeout and retry policies are honored | tests |
| E8 | Resource locks serialize conflicting writes | concurrency test |
| E9 | Tool errors preserve audit evidence | test |
| E10 | Large results become artifacts rather than prompt/log explosions | integration test |
| E11 | Secret-bearing inputs/results are redacted | tests |
| E12 | MCP STDIO tools can register and execute | contract test |
| E13 | MCP Streamable HTTP tools can register and execute | contract test |
| E14 | HTTP, Python, CLI, and node adapters share one execution contract | adapter tests |

## F. Questions and approvals

| ID | Requirement | Evidence required |
|---|---|---|
| F1 | Agent or workflow can create a typed clarification | test |
| F2 | Run pauses durably and survives restart | end-to-end test |
| F3 | Any authorized gateway can answer the same interaction | API contract test |
| F4 | Duplicate answers are rejected or safely idempotent | test |
| F5 | Approval binds exact payload/artifact hash | test |
| F6 | Changed payload invalidates approval | test |
| F7 | Rejection cancels or branches according to workflow policy | test |
| F8 | Interaction expiry is handled explicitly | test |
| F9 | Responder identity and channel are audited | test |

## G. Workflow engine

| ID | Requirement | Evidence required |
|---|---|---|
| G1 | Sequential agent/tool steps execute | test |
| G2 | Independent branches run in parallel | timing/event test |
| G3 | Join waits for required dependencies | test |
| G4 | Workflow invokes child workflow recursively | test |
| G5 | Conditions/switches are deterministic and validated | tests |
| G6 | Optional-step failure can continue with warning | test |
| G7 | Required-step failure fails parent | test |
| G8 | Retry/fallback policy works | tests |
| G9 | Timer wait survives restart | test |
| G10 | Event wait survives restart and deduplicates events | test |
| G11 | Compensation hook runs according to policy | test |
| G12 | Output mapping produces declared workflow schema | test |

## H. Automations and events

| ID | Requirement | Evidence required |
|---|---|---|
| H1 | Manual automation trigger creates pinned workflow run | test |
| H2 | Cron/interval schedule creates due run | clock-controlled test |
| H3 | Missed schedule recovery follows declared policy | test |
| H4 | Webhook event is validated, stored, deduplicated, and queued quickly | integration test |
| H5 | Duplicate external event does not duplicate work | test |
| H6 | Automation concurrency policy is enforced | test |
| H7 | Repeated failure enters dead-letter/operator state | test |
| H8 | Automation can be paused/disabled | test |

## I. Memory and artifacts

| ID | Requirement | Evidence required |
|---|---|---|
| I1 | Long-term memory is separate from run state | architecture/code inspection |
| I2 | Memory records track source and update history | tests |
| I3 | Memory access is permission scoped | denial test |
| I4 | Artifacts are immutable/versioned and content hashed | tests |
| I5 | Approval references exact artifact version/hash | test |
| I6 | Artifact access is authorized | test |
| I7 | Readable board/audit export is generated from structured state | test |

## J. Nodes and external executors

| ID | Requirement | Evidence required |
|---|---|---|
| J1 | Node enrollment uses one-time or securely rotated credential | test |
| J2 | Node advertises explicit capabilities and allowed roots | API demonstration |
| J3 | Task lease prevents duplicate simultaneous execution | test |
| J4 | Lost node lease recovers or requeues safely | test |
| J5 | Result reporting is idempotent | test |
| J6 | Codex adapter implements shared executor contract | contract test |
| J7 | Claude Code adapter implements shared executor contract | contract test |
| J8 | Executor questions route through Interaction Service | integration test |
| J9 | Workspace/path restrictions block traversal and arbitrary shell access | security tests |
| J10 | Comma/Raspberry Pi can expose read-only capabilities through node adapter | mocked contract test |

## K. Gateways and mobile UX

| ID | Requirement | Evidence required |
|---|---|---|
| K1 | iPhone Safari can create and inspect runs | manual + browser test |
| K2 | Active agent/workflow/tool status is understandable | UX review |
| K3 | Questions and approvals render inline | browser test |
| K4 | Application is installable as a PWA | manifest/service-worker test |
| K5 | Authentication is practical on mobile | manual test |
| K6 | Reconnect/offline/error states are clear | browser test |
| K7 | Live updates avoid wasteful N+1 polling | network inspection |
| K8 | Accessibility labels, contrast, focus, and touch targets pass review | accessibility test |
| K9 | Run list supports useful filtering/status | UI test |
| K10 | Raw diagnostics are available but not the primary UX | manual review |
| K11 | Telegram/Discord/WhatsApp/email/voice adapters reuse common contracts | adapter design/contract tests |

## L. Security and production

| ID | Requirement | Evidence required |
|---|---|---|
| L1 | Owner authentication exists | tests |
| L2 | Brute-force/rate-limit protection exists | tests |
| L3 | CSRF/CORS policies are explicit | tests/config inspection |
| L4 | Webhook signatures are verified | tests |
| L5 | SSRF/path/command injection boundaries are tested | security tests |
| L6 | Prompt/tool-output injection boundaries are documented and enforced | tests/review |
| L7 | Database migrations work from empty and previous schema | migration tests |
| L8 | Hosted database/files survive redeploy | deployment verification |
| L9 | Graceful shutdown does not lose accepted work | test |
| L10 | Health/readiness checks reflect real dependencies | test |
| L11 | Structured logs and metrics identify failed/stuck work | demonstration |
| L12 | Backup and restore procedure exists and is tested | documented test |
| L13 | Railway/Docker deployment matches documentation | clean deployment |

## M. Legacy migration

| ID | Requirement | Evidence required |
|---|---|---|
| M1 | Legacy agent and skill inventory is documented | migration table |
| M2 | Transport-specific rituals are not copied into native provider agents | review |
| M3 | Reusable MCP tools are migrated one by one with tests | package tests |
| M4 | Builder uses draft/validate/test/approve/publish | end-to-end test |
| M5 | Audit workflow reads structured events rather than raw board truth | test |
| M6 | Historical logs are sanitized before replay use | fixture scan |
| M7 | No historical credential is committed | secret scan |

## Required final summary format

Claude Fable 5 should finish with a table containing:

```text
ID | Status | Evidence | Remaining work | Priority
```

The final report must separate:

1. complete and tested engineering;
2. incomplete engineering;
3. integrations blocked only by account credentials;
4. intentionally deferred work;
5. recommendations rejected as unnecessary complexity.
