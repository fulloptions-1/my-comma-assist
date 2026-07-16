# Live Product Gaps Observed on iPhone

These are direct observations from the deployed product after Anthropic was configured and verified live.

## What works now

- Login from iPhone Safari.
- Persistent database on Railway volume.
- Chat, Runs, and Settings views.
- Auto/Concierge delegation to Assistant.
- Direct car-maintenance behavior.
- Questions and approvals.
- Encrypted Anthropic credential storage.
- Provider test endpoint returning `pong`.
- Active Anthropic profile.
- Successful real Anthropic responses.
- Provider-safe tool name mapping for Anthropic and OpenAI-compatible APIs.

## Product and UX failures

1. **The UI does not explain the system model.**
   The user cannot tell the difference between provider, model profile, agent, workflow, and Concierge.

2. **No useful target picker in the composer.**
   The composer mostly displays `Auto · Concierge`; the user needs an obvious selector for Agent, Workflow, Automation, and model profile.

3. **No per-run model selection.**
   The user cannot choose the model/profile for one request without changing the global active profile.

4. **The exact model is hidden.**
   Run cards show `Model anthropic`, not the profile and exact model such as `anthropic-default · claude-haiku-4-5`.

5. **“ChatGPT” is not discoverable.**
   OpenAI-compatible support exists, but there is no setup wizard explaining that the user must configure an OpenAI-compatible provider and create a profile. Provider names and model choices are confusing.

6. **Only three agents exist.**
   Concierge, Assistant, and Car Maintenance are deployed. The old swarm agents, skills, managers, builders, auditors, media agents, MCP tools, and external executors are not migrated.

7. **Assistant capability description is misleading.**
   The general Assistant currently reports mainly car-history abilities because only one domain tool is available.

8. **Duplicate answer rendering.**
   The same assistant result appears inside the run card and again in a second bubble below it.

9. **Markdown is shown as raw text.**
   Responses containing `**bold**` and list syntax are not rendered cleanly.

10. **Raw provider errors dominate the UI.**
    Failed runs expose long HTTP JSON payloads. Users need a short explanation, retry action, and diagnostics drawer.

11. **No retry or clone-run action.**
    Failed runs remain history-only and cannot be retried after correcting provider configuration.

12. **Settings is too technical and vertically long.**
    Provider credentials, profiles, active model, and advanced fields need a guided setup flow and progressive disclosure.

13. **No agent/workflow library.**
    The user cannot browse capabilities, see descriptions, versions, tools, provider requirements, status, or invoke them directly.

14. **No visual builder.**
    The user cannot create/edit/test/publish agents, skills, workflows, tools, or automations from the iPhone.

15. **No real multi-turn conversation model.**
    Each message is primarily a new run. The UI looks like chat but lacks explicit conversation threads, context policy, summaries, and controlled memory.

16. **No live background orchestration view.**
    There are event chips, but no clear tree/timeline showing parent run, child agents, tool calls, waits, joins, and background progress.

17. **Horizontal overflow and mobile polish issues.**
    Quick-action chips can clip or extend off-screen. Long responses and composer positioning need stronger mobile layout behavior.

18. **No cost/usage visibility.**
    The user cannot see tokens, estimated cost, model attempts, fallback use, or budget status per run.

## Engine gaps

- no durable background task queue/leases;
- no child runs or parent/root run tree;
- no generic workflow DAG;
- no parallel branches and joins;
- no cron/interval schedules;
- no webhook/event triggers;
- no dead-letter/operator queue;
- no MCP STDIO or Streamable HTTP adapters;
- no artifacts service;
- no generic long-term memory service;
- no remote node/external executor protocol;
- no Codex or Claude Code adapter;
- no Telegram, Discord, WhatsApp, Gmail, email, voice, Comma, or Raspberry Pi gateway;
- no safe legacy-import and publish pipeline.
