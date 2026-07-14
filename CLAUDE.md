# Atlas Engine — Claude Fable 5 Review Instructions

You are the senior architecture, reliability, security, UX, and implementation reviewer for Atlas Engine. Work only on the `atlas-claude-review` branch of this public snapshot. Do not modify `master`, do not rewrite history, and do not assume access to the private production repository or Railway secrets.

## Mission

Review the complete snapshot and transform the current Atlas vertical slice into the strongest practical foundation for the user's private, remotely accessible automation operating system. The system must support direct agent invocation, durable workflows, nested workflows, event-driven and scheduled automations, user questions and approvals, MCP tools, external executor nodes, replaceable model providers, mobile access, and safe expansion into many packages.

The user is not asking for a toy demo. They want a production-minded system that can be tested immediately, grown gradually, and understood without drowning in architecture. Preserve simplicity where it increases reliability.

## Required reading order

Before proposing or changing code, read all of the following:

1. Every tracked file in this branch.
2. `docs/FABLE5_REVIEW_BRIEF.md`
3. `docs/LEGACY_SWARM_CONTEXT.md`
4. `docs/CURRENT_ATLAS_STATE.md`
5. `docs/FABLE5_ACCEPTANCE_MATRIX.md`
6. `FABLE5_PROMPT.md`
7. Git history for this branch.

Do not assume documentation is correct. Cross-check every claim against code and tests.

## Mandatory first actions

1. Run the complete test suite before editing.
2. Run static/import checks and start the app locally.
3. Exercise the car-maintenance flow through the HTTP API:
   - direct invocation;
   - Concierge/auto resolution;
   - missing-mileage question;
   - approval;
   - idempotent write;
   - history query;
   - restart persistence.
4. Record baseline results in `docs/FABLE5_FINDINGS.md`.
5. Create `docs/FABLE5_IMPLEMENTATION_PLAN.md` with prioritized work, dependencies, risks, and acceptance tests.

## Design rules that must remain true

- Models propose decisions; deterministic code owns state, permissions, safety, retries, recovery, scheduling, budgets, and side effects.
- Explicit targets bypass routing.
- Unknown targets use a Capability Resolver; do not reintroduce a token-expensive Router agent as the default path.
- A workflow may invoke agents, tools, user interactions, timers, events, external executors, and other workflows recursively.
- A package is an organizational/deployment bundle, not an execution level.
- A manager agent is used only when ambiguous planning requires judgment; deterministic workflows coordinate known sequences.
- Agent delegation creates durable child work rather than replacing one global active agent.
- Every run has immutable event history and a current-state projection.
- Side-effect tools are idempotent.
- Approvals bind to the exact payload or artifact that will execute.
- Definitions are versioned and running jobs remain pinned to the versions they started with.
- Secrets never enter prompts, package files, normal events, logs, screenshots, or Git.
- Provider-specific parsing and recovery remain quarantined behind a provider adapter.
- The user-facing application must work well on iPhone Safari.

## Review scope

Review and improve, where justified:

- package and graph validation;
- run/event kernel and legal state transitions;
- event ordering and transaction safety;
- concurrency and locking;
- workflow DAG execution and nested child workflows;
- user questions and approvals;
- Tool Gateway permissions, schemas, timeouts, retries, idempotency, and resource locks;
- model-provider interface and low-cost reliable provider selection;
- encrypted provider-key management;
- MCP STDIO and Streamable HTTP integration design;
- schedules, webhook events, trigger deduplication, and dead-letter handling;
- long-term memory versus run state;
- artifacts and approval hashing;
- remote node protocol for Codex, Claude Code, browser automation, Comma, Raspberry Pi, and future edge devices;
- gateway architecture for web, CLI, Telegram, Discord, WhatsApp, email, and voice;
- mobile UX, accessibility, PWA behavior, offline/error states, and live progress;
- security boundaries, authorization, CSRF/CORS, rate limiting, secret redaction, and auditability;
- deployment, persistence, health checks, migration strategy, and production observability;
- fake-provider tests, replay tests, contract tests, restart tests, race tests, and end-to-end tests;
- migration of reusable agents, skills, and MCP tools from the legacy swarm.

## Implementation behavior

Do not produce only a critique. After documenting findings and plan:

1. Fix correctness and security defects first.
2. Add missing tests before or with fixes.
3. Make small, reviewable commits.
4. Preserve working behavior unless the plan explicitly replaces it.
5. Avoid speculative infrastructure. One deployable service plus durable storage is preferred until scale forces separation.
6. Do not add a message broker, Kubernetes, vector database, or microservice split without measured need.
7. Keep schemas and contracts explicit and typed.
8. Fail loudly on invalid definitions; never silently drop unknown tools, skills, targets, or duplicate IDs.
9. Never report a feature complete unless it is implemented and tested.
10. Mark intentionally deferred integrations clearly, with exact contracts and acceptance criteria.

## Required deliverables

By the end of your work, the branch must contain:

- `docs/FABLE5_FINDINGS.md`
- `docs/FABLE5_IMPLEMENTATION_PLAN.md`
- `docs/FABLE5_CHANGELOG.md`
- updated architecture and migration documentation;
- all code and tests for selected improvements;
- a final verification section containing commands and results;
- a remaining-gap list distinguishing account/credential work from missing engineering work.

## Definition of done

Do not call the review complete until:

- tests pass from a clean checkout;
- the application starts locally and in Docker;
- invalid packages are rejected with actionable errors;
- direct and auto-resolved car flows work;
- question and approval states survive restart;
- duplicate retries do not duplicate maintenance records;
- concurrent writes cannot corrupt run events or vehicle history;
- provider failures cannot corrupt run state;
- the mobile interface shows active agent, status, interactions, and event progress clearly;
- no secret is committed;
- deployment instructions match the actual repository;
- the branch can be reviewed without touching production.
