# Legacy Swarm Implementation Context

This file summarizes the original implementation that Atlas is replacing. The full historical source was supplied outside this public snapshot as a combined codebase export. Do not copy credentials, secrets, or raw historical logs into Git.

## Repository shape

The original project contained approximately:

```text
1- main.py
agent_engine.py
gemini_web.py
tools_engine.py
utils.py
mcp_config.json
agents/
skills/
servers/
logs/
OpenClaw Protocols/
```

The important execution path was:

```text
User → Orchestrator → Router → Manager → Worker(s) → Orchestrator → User
```

Only one agent was active at a time.

## `1- main.py` / `SwarmSession`

Responsibilities were combined in one blocking console runtime:

- create a timestamped session directory;
- create `board.md` before spawning MCP subprocesses;
- store `CURRENT_BOARD_PATH` in the environment;
- start `ToolsEngine` and `AgentEngine`;
- read user input through blocking `input()`;
- append user instructions directly to `board.md`;
- hold one `target` agent ID and one active model client;
- initialize a new model client on every handover;
- compile the target agent prompt;
- stream model output to the terminal;
- parse the final command block;
- execute one tool command;
- switch agents on handover;
- save Markdown logs per agent;
- detect a missing command;
- let only the Orchestrator finish with plain text;
- block for manual terminal intervention when another agent emitted no command;
- catch keyboard interruption and save the active log.

The runtime intentionally inserted random 2–3.5 second pacing sleeps before agent initialization and after tool execution. These were transport/rate-limit survival measures and are unacceptable in the normal low-latency future path.

### Recovery behavior

On a `GhostPayloadError`, absolute silence, or likely cut-off tool command, the runtime:

- printed a recovery message;
- slept;
- created a fresh Gemini web client with a randomized browser fingerprint;
- cleared external thought state;
- reset the local log buffer marker;
- replayed the agent's initial prompt.

External side effects and board writes were not rolled back. This creates replay/idempotency risk.

### Completion heuristic

Completion was not a typed runtime result. It was inferred as:

```text
active agent is orchestrator
AND response contains no tool command
AND response is non-empty
```

For every other agent, no command caused a blocking terminal prompt.

## `agent_engine.py`

### Agent lookup

Agents were Markdown files with optional YAML frontmatter. Lookup used recursive filename search and selected the first sorted match. Consequences:

- duplicate IDs were not rejected;
- generated files could shadow existing definitions;
- exact definition provenance was weak.

### Frontmatter fields

Typical fields:

```yaml
id: agent-id
name: Display Name
description: Purpose
allowed_tools: []
allowed_handovers: []
available_skills: []
```

The loader:

- looked up allowed tools in the current Tool Registry;
- silently filtered unknown tool names;
- looked up available handover targets;
- silently filtered invalid handovers;
- listed skill names without full graph validation;
- replaced template markers;
- printed initialization status.

Future validation must reject stale references instead of silently reducing capabilities.

### Tool command protocol

The model produced a final standalone block:

```text
«TOOL_COMMAND»
{
  "name": "tool_name",
  "parameters": {}
}
«END»
```

The parser executed only the final line-anchored command block. Everything above it became implicit context. Text after the final `«END»` produced a parse-repair command. Unclosed final JSON-looking commands were treated as stream cut-offs.

This parser was a thoughtful defense against generated documentation containing example command markers, but it exists because the model transport did not provide native typed tool calls. In Atlas it belongs only inside a legacy provider adapter.

## `gemini_web.py`

The backend was an unofficial guest-mode browser transport to the Gemini web application.

It contained:

- `curl_cffi` session impersonation;
- browser fingerprint rotation;
- bootstrap scraping for hidden tokens and request parameters;
- guest/anonymous headers;
- hard-coded web model IDs;
- manually constructed Bard frontend payloads;
- streamed HTTP response parsing;
- nested JSON extraction from `wrb.fr` frames;
- context IDs extracted by regex;
- a watchdog thread;
- 60-second no-output timeout;
- 120-second no-growth timeout after output began;
- captcha detection;
- burned-session/ghost-payload detection;
- token-cache invalidation and retry behavior.

The future kernel must not know any of these concepts. If retained, the entire client becomes one provider adapter returning the standard provider contract.

Concurrency is particularly risky with this transport because multiple unattended runs can multiply rate limits, captchas, session burns, and transport instability.

## `tools_engine.py`

The old Tool Engine:

- started a dedicated asyncio event loop in a daemon thread;
- spawned enabled MCP STDIO subprocesses from `mcp_config.json`;
- initialized MCP sessions;
- aggregated all tool definitions into one registry;
- exposed an internal `handover_to_agent` pseudo-tool;
- executed external tools with a global 60-second future timeout;
- injected `agent_id` into thinking tools;
- injected implicit context into file-writing tools;
- printed success/failure directly to the terminal;
- returned tool-output wrapper text to the model;
- appended handover blocks directly to `board.md`.

Handover parameter parsing accepted many aliases such as `target_agent`, `agent_id`, `agent_name`, `instruction`, `reason`, `message`, and `task`. This improved prototype tolerance but weakened strict contracts.

The future Tool Gateway must not own agent control transfer. It should enforce schemas, permissions, timeouts, retries, idempotency, locks, redaction, and audit events.

## `board.md`

`board.md` served simultaneously as:

- original user instruction store;
- cross-agent handover store;
- implicit-context transport;
- session memory;
- readable audit trail.

Writers included:

- `main.py`;
- `ToolsEngine`;
- the board MCP server.

There was no shared transactional lock. This was survivable only because the runtime was mostly serial. Concurrent runs would corrupt or lose writes, especially where whole-file replacement was involved.

Repeated tag names could return several historical sections. Agents could receive stale and current context together.

In Atlas, run events, projections, domain memory, and artifacts replace board truth. A board view may be generated read-only.

## MCP servers

The original MCP configuration enabled servers for:

- thinking;
- audit;
- board operations;
- routing/workflow discovery;
- skills;
- builder operations.

Other experiments included Notion and YouTube-related servers.

Reusable server logic should be reviewed and migrated behind the new Tool Gateway. Their tool contracts, security, dependencies, side effects, and test coverage must be evaluated individually.

## Skills

Skills were Markdown behavior/domain files. Useful concepts included:

- standard tool usage;
- orchestrator behavior;
- router behavior;
- manager behavior;
- worker behavior;
- audit behavior;
- workflow architecture guidelines;
- media writing/reviewing/shortening;
- Notion integration;
- codebase manipulation;
- workflow creation roles.

Skills are among the most reusable legacy assets after removing transport-specific rituals and pinning published versions.

## Agent definitions

Legacy agents included:

- Orchestrator;
- Router;
- Audit Manager;
- Session Auditor;
- Knowledge Distiller;
- Condensing Manager and Condenser;
- Media Manager, Writer, Reviewer, Shortener, and Sorter;
- Notion Operator;
- Workflow Creation Manager;
- Meta Router;
- Workflow Architect;
- MCP Researcher;
- Skill Generator;
- Agent Generator;
- Tool Developer;
- Workflow Condenser.

Most agent prompts forced this pattern:

1. load `standard-tool-usage`;
2. load role/domain skills one at a time;
3. read the entire board;
4. call external `think`;
5. issue exactly one tool command;
6. stop and wait for tool output.

Those instructions were transport compensation. Migrate domain knowledge and role purpose, not mandatory ritual.

## Builder workflow

The legacy self-building pipeline could generate:

- skills;
- tools;
- agents;
- workflow folders.

Historical logs demonstrated several failure modes:

- multiple command blocks in one response;
- wrong tool parameter names;
- malformed JSON;
- generated Python corrupted by Markdown-link rewriting or escaping;
- hard-coded secrets;
- tools reported as successfully implemented when the actual file was incomplete or invalid;
- new files written directly into live locations without a full publish barrier.

The future builder process must use:

```text
draft → validate → test → security review → human approval → publish immutable version
```

Generated code must never be trusted based on an agent's prose claim.

## Audit and logs

The old system generated large Markdown logs of every prompt, tool output, and response. These contain valuable examples of:

- successful routing;
- malformed commands;
- provider cut-offs;
- ghost-payload recovery;
- duplicate tool attempts;
- generated-code corruption;
- prompt-protocol failures.

They are useful as sanitized replay fixtures, not automatically correct expected outputs. Historical credentials and secrets must be removed and rotated.

## Known design defects to prevent in Atlas

- one globally active agent;
- blocking console input as a control primitive;
- model-specific transport behavior shaping the whole engine;
- replay after recovery without side-effect idempotency;
- non-transactional multi-writer board state;
- stale context from repeated board tags;
- silent duplicate agent shadowing;
- silent missing-reference filtering;
- one global tool timeout;
- no explicit completion contract;
- no durable pause/resume;
- no version pinning;
- no package-wide validation;
- no resource locks;
- no approval-artifact binding;
- secrets retained in prompts and logs;
- builder agents writing directly to production definitions;
- serial routing token overhead for explicit targets.

## Migration principle

Preserve:

- domain skills;
- useful role instructions;
- MCP tool logic after review;
- audit concepts;
- builder concepts;
- historical failure fixtures;
- defensive provider parsing inside the legacy adapter.

Replace:

- Router agent with Capability Resolver;
- Orchestrator with a non-blocking Concierge;
- workflow folders with packages;
- handover control transfer with durable child work;
- board truth with database events/projections/memory;
- console session with API, gateways, and workers;
- heuristic completion with explicit typed completion;
- live file generation with validated versioned publication.
