# Atlas Next Acceptance Matrix

Use PASS, PARTIAL, FAIL, ACCOUNT-BLOCKED, or DEFERRED. Evidence must be executable.

## M1.6 — Product and mobile experience

| ID | Requirement | Evidence |
|---|---|---|
| UX1 | Composer can select Auto, exact Agent, Workflow, or Automation | browser/API tests |
| UX2 | Composer can select an optional per-run model profile | tests showing pinned override |
| UX3 | Run card shows entry target, effective agent, provider, profile, exact model, usage and cost | browser/API tests |
| UX4 | Agent/workflow library lists capabilities, versions, tools and status | browser tests |
| UX5 | Provider setup wizard supports Anthropic and OpenAI-compatible profiles | end-to-end tests |
| UX6 | Friendly errors replace raw JSON; diagnostics remain accessible | browser tests |
| UX7 | Failed run can be retried/cloned safely | idempotency and UI tests |
| UX8 | Assistant response is rendered once and Markdown is sanitized | browser tests/security tests |
| UX9 | Settings uses progressive disclosure and works on iPhone widths | screenshot/browser tests |
| UX10 | Conversation threads support controlled multi-turn context | restart and context tests |
| UX11 | Cost/token/budget/fallback information is visible | API/UI tests |
| UX12 | PWA install, safe areas, reconnect and offline shell work | PWA/browser tests |

## M2 — Durable execution and workflows

| ID | Requirement | Evidence |
|---|---|---|
| EX1 | Background task records have claims, owners, leases and attempts | concurrency/restart tests |
| EX2 | Parent/root/child runs are durable and queryable as a tree | tests/API |
| EX3 | Parent wake-up cannot be lost when child completes during wait transition | repeated race test |
| EX4 | Cancellation propagates by declared policy | tests |
| WF1 | Versioned workflow DAG schema is validated | cycle/orphan/schema tests |
| WF2 | Agent and tool steps execute sequentially | integration test |
| WF3 | Parallel branches and join work | timing/event tests |
| WF4 | Workflows invoke child workflows recursively | restart test |
| WF5 | Conditions, retry, fallback and failure policies work | tests |
| WF6 | Questions and approvals pause and resume workflow steps | restart tests |
| WF7 | Timer and event waits survive restart | controlled-clock/event tests |
| WF8 | Output mapping validates declared schema | tests |

## Automations and integrations

| ID | Requirement | Evidence |
|---|---|---|
| AU1 | Manual, interval and cron triggers create pinned workflow runs | tests |
| AU2 | Signed webhooks are normalized, persisted and deduplicated | tests |
| AU3 | Automation concurrency and missed-schedule policy work | tests |
| AU4 | Repeated failures enter operator/dead-letter state | tests/UI |
| MCP1 | MCP STDIO adapter discovers and executes typed tools | fake MCP contract test |
| MCP2 | MCP Streamable HTTP adapter discovers and executes typed tools | fake MCP contract test |
| NODE1 | Secure node enrollment, capability advertisement and task leases work | contract tests |
| NODE2 | Mock Codex and Claude Code executors use one contract | tests |
| GATE1 | Shared gateway message contract exists | tests |
| GATE2 | At least one credential-free mocked external gateway works end-to-end | integration test |

## Builder and migration

| ID | Requirement | Evidence |
|---|---|---|
| BLD1 | Draft → validate → test → approve → immutable publish works | end-to-end test |
| BLD2 | Invalid package cannot replace last published version | rollback test |
| BLD3 | iPhone builder can create and edit agent/workflow definitions | browser tests |
| MIG1 | Legacy importer inventories migrated/rejected/manual-review items | fixture test |
| MIG2 | Transport rituals are removed from native-provider prompts | review/test |
| MIG3 | No secret from a legacy archive reaches Git/events/prompts | security tests |
