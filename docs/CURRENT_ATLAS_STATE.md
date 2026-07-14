# Current Atlas State — Review Baseline

This document describes the code included in this public review snapshot. Verify every statement against source before relying on it.

## Topology

The private production version is deployed as one Railway service from a private GitHub repository. It builds from the root Dockerfile and serves a FastAPI application through Uvicorn. Railway generated a public HTTPS domain.

The current database is SQLite at `ATLAS_DB_PATH`, defaulting to `/data/atlas.db` in Docker. Persistence across Railway replacement/redeploy depends on an attached persistent volume. Do not assume `/data` is durable merely because the path exists.

This public branch is an isolated, secret-free review snapshot. It is not connected to Railway production.

## Current source files

```text
atlas/
├── app.py
├── db.py
├── registry.py
├── runtime.py
└── tools.py

packages/
├── core/
│   ├── manifest.yaml
│   └── agents/concierge.yaml
└── car/
    ├── manifest.yaml
    └── agents/car-maintenance.yaml

tests/test_vertical_slice.py
Dockerfile
railway.toml
requirements.txt
```

## `atlas/app.py`

Implemented:

- FastAPI application;
- `/health`;
- create, list, and inspect run endpoints;
- interaction-answer endpoint;
- embedded mobile HTML/CSS/JavaScript interface;
- direct target selector;
- Auto/Concierge selector;
- run cards;
- inline questions and approvals;
- polling-based refresh.

Not implemented:

- authentication;
- authorization;
- sessions;
- CSRF/CORS policy;
- rate limiting;
- provider Settings UI;
- encrypted API-key entry;
- package/agent/workflow builder;
- PWA manifest/service worker;
- server-sent events or WebSocket live streaming;
- pagination or efficient batched run detail retrieval;
- robust offline/reconnect behavior;
- multi-user conversation model.

## `atlas/db.py`

Implemented SQLite tables:

- `runs`;
- `run_events`;
- `interactions`;
- `tool_executions`;
- `maintenance_events`.

Implemented behavior:

- WAL mode;
- `busy_timeout`;
- `BEGIN IMMEDIATE` write transactions;
- monotonically increasing per-run event sequence;
- run projections stored in `runs`;
- append-only event inserts;
- durable pending interactions;
- persistent maintenance records;
- basic indexes.

Not implemented:

- migration framework;
- Postgres support;
- explicit state-transition validation;
- task/step table;
- child runs or parent/root IDs;
- artifacts;
- memory service beyond the car domain table;
- automation/event tables;
- provider configuration and encrypted secrets;
- node enrollment/tasks/leases;
- budgets;
- dead-letter records;
- backup/retention policy;
- ownership/tenant columns;
- event schema versioning;
- projection rebuild command.

The current `transition` method accepts arbitrary state strings and does not verify legal transitions. Review transaction boundaries, error behavior, and data integrity under simultaneous requests.

## `atlas/registry.py`

Implemented:

- package manifest discovery under `packages/*/manifest.yaml`;
- agent YAML loading;
- duplicate agent ID rejection;
- unknown tool rejection;
- required handler check;
- basic immutable `AgentBlueprint` data class;
- exact agent lookup;
- simple intent substring scoring;
- fallback to `concierge-agent`.

Not implemented:

- JSON Schema for package files;
- workflow, automation, skill, model profile, policy, or tool-definition loading;
- version graph and immutable publication;
- duplicate detection across definition types;
- delegation-target validation;
- dependency/cycle validation;
- semantic matching;
- permissions/availability filtering;
- package reload transaction;
- draft versus published state;
- package signatures or provenance;
- actionable line-number diagnostics.

## `atlas/runtime.py`

Implemented:

- create a run for an explicit agent or Auto resolver;
- synchronous advancement inside the HTTP request;
- Concierge resolution;
- car-maintenance deterministic handler;
- car-history query;
- event-type extraction from plain text;
- mileage extraction;
- durable question interaction;
- durable approval interaction;
- approval payload hashing;
- tool call and final completion;
- cancellation on rejected approval.

Important architectural fact:

The current `AgentBlueprint` is not executed through an LLM. `handler` selects a hard-coded Python method. There is no model-provider interface, agent compiler, prompt construction, typed model tool-call loop, token accounting, or provider key.

The current Concierge does not create a child run. It records delegation events and executes the selected hard-coded handler within the same run.

Not implemented:

- background worker queue;
- concurrent runs beyond simultaneous HTTP threads;
- workflow engine;
- child runs;
- task leases;
- cancellation signal;
- retries/timeouts/fallbacks;
- budgets;
- model providers;
- output schemas;
- external executors;
- event/schedule waits;
- compensation;
- version pinning beyond the single stored agent version field;
- recovery of partially advanced runs after process restart;
- explicit resumption scan for stuck `RUNNING` runs.

Review whether errors during interaction resolution can leave an interaction resolved while the run fails to advance, and whether the sequence of state transition versus interaction creation can expose short inconsistent windows.

## `atlas/tools.py`

Implemented tools:

- `car.read_history`;
- `car.log_maintenance`.

Implemented Tool Gateway behavior:

- tool registry;
- logical idempotency key;
- input hash conflict detection;
- durable execution record;
- tool started/completed/failed events;
- duplicate maintenance-event defense.

Not implemented:

- JSON Schema validation;
- output validation;
- agent/workflow permission enforcement;
- credentials by reference;
- timeouts;
- retries/backoff;
- error categorization;
- resource locks;
- result size limits;
- artifact storage;
- secret redaction;
- MCP STDIO;
- MCP Streamable HTTP;
- HTTP/CLI/node adapters;
- per-tool policy and versioning.

## Package definitions

### `concierge-agent`

- deterministic handler: `concierge`;
- no tools;
- no intents.

### `car-maintenance-agent`

- deterministic handler: `car_maintenance`;
- intents for oil, tires, service, and renewal;
- allowed tool names listed in YAML;
- no real prompt, skills, model profile, input schema, output schema, permissions, or delegation policy.

## Tests

The current suite contains three vertical-slice tests:

1. missing-mileage question → approval → idempotent maintenance write;
2. maintenance history surviving creation of a new Runtime over the same SQLite file;
3. unknown-tool package rejection.

Missing critical tests include:

- HTTP/UI end-to-end tests;
- legal state transitions;
- simultaneous event appends;
- simultaneous writes to one vehicle;
- restart while waiting for question;
- restart after approval but before tool completion;
- process death after side effect but before success event;
- malformed interaction answer;
- approval hash mismatch;
- duplicate interaction submission;
- provider contract/failure tests;
- workflow/child-run tests;
- migration tests;
- Docker clean-build smoke test;
- security tests.

## Current strengths

- Small code surface that is understandable.
- Clear separation between database, registry, runtime, tools, and API compared with the legacy swarm.
- Durable events and interactions for the implemented path.
- Strict duplicate and unknown-tool handling for current agent definitions.
- Basic idempotency for the car write.
- Working Docker/Railway build in the private production repository.

## Current gaps versus the full vision

The version is a vertical slice, not the complete automation platform. The following are still missing engineering work, not merely account configuration:

- real model provider and key management;
- generic agent compiler/runtime;
- generic workflow engine;
- recursive child workflows;
- automations, cron, and event ingestion;
- provider/tool budgets;
- complete package builder and validator;
- skills and reusable prompt composition;
- MCP adapters;
- artifact service;
- generic memory service;
- remote node protocol;
- Codex and Claude Code adapters;
- external gateway adapters;
- authentication and security hardening;
- Postgres/migrations and production durability;
- observability/operator/dead-letter views;
- migration of legacy agents, skills, and MCP tools.

Account-specific integrations additionally require credentials or authorization for Gmail, Telegram, Discord, WhatsApp, provider APIs, Codex, Claude Code, and external services.

## Review priority suggestion

1. Verify durability and security of the vertical slice.
2. Add provider abstraction, encrypted settings, and fake-provider contract tests.
3. Add explicit state machine, tasks, workers, and restart recovery.
4. Add generic workflows and child-run semantics.
5. Expand package validation/versioning.
6. Add MCP Tool Gateway adapters.
7. Add automations and event ingestion.
8. Add nodes/external executors.
9. Migrate legacy capabilities package by package.
