# Copy-paste prompt for Claude Fable 5 / Claude Code

Use the public repository `fulloptions-1/my-comma-assist` and check out branch `atlas-claude-review`.

You are the principal engineer responsible for reviewing, correcting, hardening, and extending Atlas Engine. This branch is a standalone public snapshot. Ignore the unrelated historical `master` branch and work only from the files visible on `atlas-claude-review`.

Read root `CLAUDE.md`, every tracked file, and all documents under `docs/` before changing code. Cross-check documentation against implementation. Do not assume a claimed feature exists merely because it is described.

The original system was a blocking single-active-agent Python console swarm using YAML-frontmatter agent Markdown files, skills, MCP subprocesses, `board.md`, handover control transfer, and an unofficial Gemini web backend with ghost-payload, tarpit, cut-off, pacing, and fingerprint-recovery logic. The new target is a small deterministic automation kernel with durable event-log-driven runs, versioned definitions, direct invocation, a non-blocking Concierge, a deterministic Capability Resolver, typed tools, recursive workflows, automations, questions, exact approvals, replaceable model providers, MCP adapters, remote nodes, external coding executors, mobile gateways, and strong testing.

The current snapshot is only a car-maintenance vertical slice. It has FastAPI, SQLite run events, interactions, basic package validation, two deterministic agents, two car tools, idempotent maintenance logging, Docker/Railway configuration, and a mobile UI. It does not yet contain the complete provider runtime, workflow engine, automations, MCP adapters, external nodes, encrypted model settings, production authentication, Postgres migrations, or full legacy migration. Verify the exact state from code.

Your task is not to generate a high-level opinion and stop. You must:

1. Establish a clean baseline by installing dependencies, running tests, starting the app, and exercising direct and Auto car flows.
2. Write `docs/FABLE5_FINDINGS.md` with factual code-referenced findings, severity, reproduction steps, and evidence.
3. Write `docs/FABLE5_IMPLEMENTATION_PLAN.md` with prioritized vertical slices, dependencies, risks, and acceptance tests.
4. Fix correctness, durability, security, provider-boundary, deployment, and mobile-UX defects that should be addressed now.
5. Add tests before or with every critical change.
6. Implement the smallest coherent next platform layer rather than scattered stubs. Prioritize:
   - explicit run state machine and recovery;
   - package/version validation;
   - provider interface plus deterministic fake provider and one native low-cost provider adapter;
   - encrypted provider-key settings without exposing keys;
   - generic agent compiler/runtime with typed tool calls;
   - Tool Gateway schema/permission/idempotency/locking policies;
   - durable task/worker semantics;
   - generic workflow DAG and child-run behavior;
   - restart and race tests;
   - persistent hosted storage and migrations;
   - authentication and mobile usability.
7. Preserve direct invocation and do not reintroduce a default Router-agent token chain.
8. Preserve the rule that workflows are recursive and packages are organizational bundles.
9. Treat manager agents as optional judgment components, not mandatory hierarchy nodes.
10. Quarantine any legacy Gemini web parsing/recovery inside a provider adapter if retained.
11. Never commit secrets, copy legacy credentials, or expose API keys in prompts, events, logs, API responses, or screenshots.
12. Keep work on `atlas-claude-review`; do not merge or push to `master`.
13. Make small, reviewable commits.
14. Update `docs/FABLE5_CHANGELOG.md` with every implemented change and exact verification commands/results.
15. End with the acceptance matrix completed as PASS, PARTIAL, FAIL, ACCOUNT-BLOCKED, or DEFERRED, with evidence.

Non-negotiable architecture rule:

> Models propose decisions. Deterministic code owns state, execution, permissions, retries, scheduling, safety, recovery, budgets, approvals, and side effects.

Do not add Kubernetes, a message broker, a vector database, microservices, or speculative abstraction unless current requirements and measured behavior prove they are necessary. Prefer one deployable service and durable database until scale forces separation.

Do not claim the whole platform complete when only interfaces or demos exist. Clearly separate implemented engineering, credential-blocked integrations, and deferred work.

Start by reading `CLAUDE.md`, then follow its required reading and baseline procedure exactly.
