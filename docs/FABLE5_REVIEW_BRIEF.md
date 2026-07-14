# Claude Fable 5 Full-System Review Brief

## 1. User vision

The user is building a private automation operating system that behaves like a digital company. The platform should be accessible from an iPhone, home PC, work PC, and any authorized remote computer. It should accept direct prompts, API calls, application messages, schedules, webhooks, and edge-device events.

The long-term platform should allow the user to:

- call a specific agent directly when the target is known;
- talk to a main Concierge when the target is not known;
- create, edit, validate, version, test, and publish agents;
- create deterministic workflows combining agents, tools, approvals, timers, event waits, and child workflows;
- combine workflows recursively without inventing a new engine type at each hierarchy level;
- attach triggers to workflows to create automations;
- run multiple independent jobs concurrently;
- see which agents, workflows, tools, nodes, and external executors are active in real time;
- pause any run to request clarification or approval from the user;
- answer a question from the web app, phone notification, Telegram, Discord, WhatsApp, email, or another authorized gateway;
- connect MCP servers and ordinary Python, HTTP, CLI, and remote-node tools behind one Tool Gateway;
- connect external coding harnesses such as Codex and Claude Code without rebuilding their internal agent systems;
- use a home Windows PC with an RTX 5090 as an optional GPU/desktop worker rather than making it the single point of failure;
- connect edge nodes such as a Comma device and Raspberry Pi as capability providers and event sources;
- schedule recurring jobs and react to new-email, webhook, calendar, GitHub, sensor, or custom events;
- preserve complete run history, recover after restarts, and debug by replaying historical events;
- start lean and expand safely into many packages and organizational namespace paths.

The target mental model is:

```text
Messages / API / Events / Schedules
                │
                ▼
       Direct target or Concierge
                │
                ▼
             Durable run
    ┌───────────┼───────────┐
    ▼           ▼           ▼
  Agent       Workflow   External executor
    │           │           │
    └────── Tool Gateway ────┘
                │
                ▼
     MCP / APIs / Nodes / Data
                │
                ▼
    Events, projections, memory, artifacts
```

## 2. Core vocabulary

Use a small, stable vocabulary:

- **Model Provider**: standardized connection to a model backend.
- **Agent Blueprint**: versioned definition of one intelligent worker.
- **Skill**: reusable instructions or domain knowledge.
- **Tool**: narrow typed deterministic action.
- **Workflow**: durable graph whose steps may be agents, tools, child workflows, user interactions, timers, event waits, conditions, or external executors.
- **Automation**: published workflow plus triggers, budgets, and policy.
- **Run**: one execution of an agent, workflow, automation, or executor task.
- **Package**: deployable bundle containing related agents, workflows, skills, tools, policies, automations, and tests.
- **Namespace path**: optional organizational path such as `personal/mobility/car`.
- **Gateway**: adapter for web, API, CLI, Telegram, Discord, WhatsApp, email, or voice.
- **Node**: connected computer or edge device advertising capabilities.
- **External Executor**: complete external harness such as Codex or Claude Code.

Do not invent `workflow group`, `workflow group group`, `super-manager`, or a new runtime type for each organizational level. Workflows and namespaces are recursive.

## 3. Governing architecture rule

> Models propose decisions. Deterministic code owns state, execution, permissions, retries, safety, scheduling, recovery, and side effects.

Use agents only where judgment is needed. Use code, graph steps, conditions, validation, and typed tools for predictable work.

Examples:

```text
Known email process:
fetch → classify agent → validate code → approval → send tool
```

```text
Ambiguous complex planning:
travel manager agent decides which child workflows to launch
```

The workflow engine is the default coordinator. An LLM manager is exceptional, not mandatory.

## 4. Invocation behavior

### Explicit target

When an application or user already knows the agent or workflow ID, invoke it directly. Do not route through the Concierge or a Router agent.

```json
{
  "target_type": "agent",
  "target_id": "car-maintenance-agent",
  "input": {"message": "When was my last oil change?"}
}
```

### Unknown target

A general conversation goes to the Concierge. The Concierge calls a deterministic Capability Resolver that searches metadata, intents, permissions, availability, and optionally semantic embeddings. A small-model classifier is a fallback, not the first step.

The old token-expensive Router agent should not return as the default path.

## 5. Run and event model

A run must survive process and machine restarts. The database, not in-memory Python state or Markdown files, is authoritative.

Recommended current-state tables:

- `runs`
- `run_events`
- `tasks`
- `interactions`
- `tool_executions`
- `artifacts`
- `definition_versions`
- `automation_triggers`
- `node_tasks`
- domain tables such as `maintenance_events`

Every meaningful transition appends an immutable event:

```text
run.created
run.queued
run.started
agent.started
model.requested
model.completed
tool.requested
tool.started
tool.completed
interaction.requested
run.waiting_for_user
interaction.resolved
run.resumed
child_run.created
child_run.completed
run.completed
run.failed
run.cancelled
```

Current state is maintained as a projection for fast reads. Event sequence allocation must be transaction-safe under concurrency.

Legal run states should be explicit, for example:

```text
CREATED
QUEUED
RUNNING
WAITING_FOR_USER
WAITING_FOR_APPROVAL
WAITING_FOR_TIME
WAITING_FOR_EVENT
WAITING_FOR_CHILDREN
COMPLETED
FAILED
CANCELLED
```

Terminal runs cannot re-enter execution.

## 6. Delegation and agent communication

The legacy handover replaced the globally active agent. That must not be the future primitive.

Future delegation means:

- create a child task or child run;
- pass a structured objective and input references;
- pin the target definition version;
- define expected output schema;
- allow the parent to continue or wait explicitly;
- collect the child result through durable state.

Prefer structured outputs and artifact references over free-form agent chats.

Example:

```json
{
  "target": {"type": "agent", "id": "invoice-reviewer", "version": 4},
  "objective": "Review the extracted invoice for inconsistencies",
  "input_artifacts": ["artifact-721"],
  "expected_output_schema": "invoice_review_v1"
}
```

## 7. Package system and validation

The package validator is a primary safety boundary. Invalid packages must fail before execution.

Validate at least:

- YAML/JSON syntax and schema;
- duplicate IDs across all packages;
- missing tools, skills, agents, workflows, automations, model profiles, and versions;
- invalid delegation targets;
- workflow dependency cycles;
- unreachable or orphaned workflow steps;
- invalid branch conditions;
- impossible state transitions;
- permission mismatches;
- missing credentials by reference, without reading secret values;
- tool schema incompatibility;
- trigger configuration and duplicate subscriptions;
- package manifest consistency;
- test requirements for publishable packages.

Never silently filter or drop unknown references.

Published versions should be immutable. A run pins every agent, workflow, skill, tool contract, model profile, and package version it starts with.

## 8. Provider boundary

The model layer must be replaceable.

A provider receives a standard request containing:

- messages;
- tool schemas;
- model profile;
- structured output schema;
- cancellation signal;
- budget information;
- optional cache/session metadata.

It returns a standard stream or result containing:

- text deltas;
- structured tool calls;
- final structured output;
- finish reason;
- usage and cost;
- provider error category.

Provider-specific concepts must not leak into the kernel.

The legacy Gemini web transport may be retained only as a quarantined provider adapter. Its command-marker parsing, watchdogs, session resets, captcha handling, fingerprint rotation, and cut-off recovery must remain inside that adapter.

The hosted product should include a low-cost reliable native API provider and a secure Settings flow for entering a key. Keys must be encrypted at rest, redacted from logs, and retrieved only when making an authorized provider call.

The deterministic fake provider is mandatory for tests and no-key experimentation.

## 9. Tool Gateway

One Tool Gateway should normalize:

- built-in Python tools;
- MCP STDIO tools;
- MCP Streamable HTTP tools;
- ordinary HTTP APIs;
- CLI programs;
- remote node capabilities;
- external executor adapters.

Every tool definition should declare:

- ID and version;
- input and output schema;
- required permissions;
- timeout;
- retry policy;
- retryable error classes;
- side-effect flag;
- idempotency requirement;
- resource lock key template;
- credential references;
- result-size and artifact policy;
- audit/redaction policy.

Side-effect tools must use stable logical idempotency keys. A repeated key with identical input returns the previous result; the same key with different input is rejected.

Examples needing idempotency:

- sending an email;
- creating a calendar event;
- posting a message;
- writing a maintenance event;
- creating a GitHub issue;
- publishing a report;
- making a purchase or payment.

Resource locks should serialize conflicting writes, for example:

```text
vehicle:genesis-2016
gmail-thread:abc123
invoice:2026-881
project:alpha
```

## 10. User questions and approvals

Any agent or workflow must be able to pause durably and ask the user for missing information.

A user interaction record should contain:

- interaction ID;
- run and step ID;
- kind: clarification, selection, approval, authentication, or confirmation;
- prompt;
- typed response schema;
- allowed channels;
- expiration policy;
- exact payload or artifact hash for approvals;
- response and responder identity;
- resolution time.

The run changes to a waiting state. Any authorized gateway may answer. The exact step resumes after validation.

Approval must bind to the exact payload or immutable artifact hash that will execute. If the content changes, prior approval becomes invalid.

## 11. Workflow engine

A workflow is a versioned DAG. Supported step types should eventually include:

- agent;
- tool;
- workflow;
- condition/switch;
- parallel group;
- join;
- user question;
- approval;
- timer;
- event wait;
- external executor;
- compensation step;
- final output mapping.

Each step defines or inherits:

- timeout;
- retry attempts and backoff;
- retryable errors;
- fallback;
- parent-failure policy;
- cancellation behavior;
- budget contribution;
- idempotency and lock policy.

Possible failure policies:

```text
fail_parent
continue_with_warning
skip_optional_step
use_fallback
wait_for_operator
compensate_previous_steps
```

The engine must handle the race where a child completes immediately before or during the parent's transition to `WAITING_FOR_CHILDREN`. Wake-up signals must be durable, not dependent on in-memory timing.

## 12. Automations, schedules, and events

An automation combines:

- pinned workflow version;
- trigger definitions;
- permissions;
- budgets;
- concurrency policy;
- deduplication policy;
- retry/dead-letter policy;
- owner and enabled state.

Trigger sources include:

- manual/API;
- cron or interval schedule;
- Gmail or email webhook;
- calendar change;
- GitHub webhook;
- file upload;
- Telegram/Discord/WhatsApp message;
- car metadata update;
- Raspberry Pi sensor event;
- node health event;
- custom webhook.

Webhook handlers should validate, normalize, persist, deduplicate, enqueue, and return quickly. Do not execute a full workflow inside the webhook request.

A special agent heartbeat loop is unnecessary; recurring work uses schedules. Infrastructure heartbeat signals remain necessary for node liveness and task leases.

## 13. Memory and artifacts

Run state and long-term memory are different.

Long-term structured memory examples:

- car profile and maintenance history;
- user preferences;
- company writing standards;
- project metadata;
- known contacts;
- travel preferences;
- connector configuration references.

Memory writes should track source, verification state, confidence, update history, namespace, and permission policy.

Artifacts should be immutable or versioned, content-hashed, permission-controlled, and referenced by ID. Examples include reports, PDFs, email drafts, invoice extractions, code patches, screenshots, and structured agent outputs.

`board.md` may be generated as a readable projection, but it must not be the database.

## 14. Nodes and external executors

A node advertises capabilities, health, allowed roots, resource limits, and current leases.

Examples:

```text
home-pc:
  gpu.inference
  codex.execute
  claude-code.execute
  browser.execute
  filesystem.project-alpha

comma-four:
  car.current_state
  car.trip_metadata

raspberry-pi:
  sensor.temperature
  camera.capture
  relay.switch
```

External coding harnesses should be adapters behind a shared contract:

```text
submit_task
stream_progress
send_answer
cancel_task
get_status
collect_artifacts
```

They should run in a restricted workspace, Git worktree, container, VM, or explicitly allowed Windows path. They are not general remote shells.

## 15. Gateways and conversations

External channels should normalize to a common internal message:

```json
{
  "type": "message.received",
  "channel": "telegram",
  "user_id": "owner",
  "conversation_id": "conv-12",
  "text": "Check my car maintenance",
  "attachments": []
}
```

Gateways include web/PWA, REST API, CLI, Telegram, Discord, WhatsApp, email, and voice. They reuse the same Conversation, Run, Interaction, and Approval services.

## 16. Mobile UX requirement

The iPhone application is a primary interface, not an admin afterthought.

It should provide:

- chat-style task creation;
- direct target selection when desired;
- clear active-run cards;
- agent/workflow/tool status in human language;
- live event feed;
- questions and approvals inline;
- runs view with filtering;
- settings for provider selection and API key entry;
- package/agent/workflow builder over time;
- accessibility, large touch targets, safe-area support, error recovery, and loading states;
- installable PWA behavior;
- no requirement to manipulate code from the phone.

Raw database state should be hidden behind an optional diagnostics view, not the main experience.

## 17. Security baseline

Review at least:

- owner authentication and session security;
- access-code storage and rotation;
- CSRF and CORS;
- API rate limits and brute-force protection;
- secret encryption and key rotation;
- secret redaction from events, errors, prompts, and UI;
- provider-key permissions;
- webhook signature verification;
- node enrollment and one-time tokens;
- task leases and replay protection;
- least-privilege tool permissions;
- approval binding;
- path traversal and command injection in external executors;
- SSRF in HTTP/MCP connectors;
- malicious tool output and prompt injection boundaries;
- artifact authorization;
- audit-event immutability;
- database backup and migration safety;
- old leaked credentials in history and fixtures.

Never commit real keys. Never copy historical secrets from legacy logs.

## 18. Budgets and loop prevention

Every run and automation should support limits such as:

- maximum runtime;
- maximum model input/output tokens;
- maximum estimated model cost;
- maximum tool calls;
- maximum child runs;
- maximum retry attempts;
- maximum external executor duration;
- maximum concurrency per automation or entity.

When a budget is reached, pause for operator action or fail clearly. Do not allow silent overnight loops.

## 19. Testing strategy

Required test layers:

### Unit

- state transitions;
- event reducer/projection;
- sequence allocation;
- validation;
- capability matching;
- budget enforcement;
- permissions;
- idempotency;
- resource locking;
- workflow graph readiness and joins.

### Provider contract

- deterministic fake provider;
- native provider adapter;
- legacy web adapter if retained;
- cancellation, timeout, malformed call, truncated response, rate limit, and usage reporting.

### Tool contract

- schema validation;
- permission denial;
- timeout/retry;
- idempotent side effect;
- conflicting idempotency input;
- lock contention;
- secret redaction.

### Integration

- run kernel plus workers;
- question/approval restart;
- child completion race;
- workflow parallelism;
- event-trigger deduplication;
- node disconnect and lease recovery;
- package save validation and rollback.

### End-to-end

At minimum:

```text
create maintenance run
→ ask mileage
→ restart
→ answer
→ request exact approval
→ restart
→ approve
→ tool writes exactly once
→ complete
→ query history
```

Use sanitized legacy logs as replay inputs only after labeling expected correct behavior.

## 20. Lean deployment path

Initial production should remain simple:

```text
Railway or equivalent
├── one Atlas API/worker service
├── durable volume or Postgres
└── HTTPS domain

Home Windows PC
└── optional enrolled node
```

Do not introduce a message broker or microservice split until measured load or durability requirements force it.

Production needs:

- reliable persistent storage;
- schema migrations;
- health/readiness checks;
- startup validation;
- structured logging;
- backups;
- graceful shutdown;
- safe deployment migrations;
- monitoring for failed/stuck runs;
- dead-letter/operator view.

## 21. Migration objective

Reusable legacy content includes:

- agent role instructions;
- domain skills;
- MCP server implementations;
- tool descriptions;
- builder and audit concepts;
- workflow examples;
- sanitized logs as failure/replay fixtures.

Do not mechanically copy legacy prompt-survival rituals into the new kernel. Preserve them only inside a legacy provider adapter if still needed.

## 22. Review output expected from Claude Fable 5

The review must answer:

1. What is actually implemented today?
2. Which documentation claims are inaccurate?
3. Which correctness, concurrency, security, persistence, provider, deployment, and UX failures exist?
4. Which abstractions are unnecessary?
5. Which missing contracts will cause future rewrites?
6. What is the smallest reliable architecture that satisfies the vision?
7. What should be fixed immediately, next, later, or explicitly deferred?
8. Which legacy agents, skills, and tools can be migrated safely?
9. What tests prove each critical guarantee?
10. Can a clean checkout deploy successfully and be used from iPhone Safari?

Then implement the highest-value improvements, verify them, and leave a transparent remaining-gap list.
