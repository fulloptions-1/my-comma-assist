# Start Prompt — Atlas M1.6 and M2

Open the public repository `fulloptions-1/my-comma-assist` and check out branch:

`atlas-fable-m2-current`

This is the secret-free snapshot of the currently deployed Atlas source after M1.5 and the live Anthropic tool-name hotfix. Ignore every other branch.

Read `CLAUDE.md`, every tracked file, `docs/LIVE_PRODUCT_GAPS.md`, and `docs/NEXT_ACCEPTANCE_MATRIX.md` before editing.

Your mission is to turn the working but narrow M1.5 foundation into a product the user can understand and operate from iPhone, then implement the durable multi-agent/workflow core.

## Phase A — M1.6 product correction

Implement, not merely design:

1. A composer target picker supporting Auto/Concierge, exact Agent, Workflow, and Automation.
2. Optional per-run model-profile selection with durable pinning; global active profile remains the default.
3. Clear run identity showing entry target, effective agent, provider, profile, exact model, fallback, token use, estimated cost, budget and status.
4. An Agent & Workflow Library with search, descriptions, versions, tools, status, direct invocation and detail pages.
5. A guided provider/model setup wizard:
   - Anthropic;
   - OpenAI-compatible, including OpenAI API configuration;
   - test key;
   - create profile;
   - select model;
   - activate profile;
   - explain provider vs model vs profile in plain language.
6. Friendly error summaries, retry/clone-run action, and a separate diagnostics drawer containing the technical error.
7. Remove duplicated assistant output. Render sanitized Markdown safely.
8. Redesign Settings using progressive disclosure and mobile-first layouts.
9. Add explicit conversation threads with controlled multi-turn context, context-size limits, summaries, and durable restart behavior. Do not silently dump all historical runs into prompts.
10. Improve background activity UI to display run/agent/tool/wait events as a readable timeline/tree.
11. Add token/cost/budget/fallback visibility.
12. Add PWA manifest/service worker, iPhone installation support, reconnect behavior and a safe offline shell.
13. Preserve the existing live design direction; do not replace it with a generic admin dashboard.

## Phase B — M2 durable execution

Implement a generic execution layer with:

- durable task table;
- worker/process owner and leases;
- retry attempts and backoff;
- parent/root/child run relationships;
- durable child completion and parent wake-up;
- cancellation propagation;
- restart recovery;
- operator/dead-letter state;
- concurrency and race tests.

Then implement a versioned recursive workflow DAG supporting:

- agent;
- tool;
- child workflow;
- condition/switch;
- parallel branches;
- join;
- question;
- approval;
- timer wait;
- event wait;
- retry/fallback/failure policy;
- output mapping and schema validation.

Explicit targets must bypass Concierge. Known deterministic workflows must not require manager-agent reasoning. Manager agents remain optional for ambiguous planning.

## Phase C — automations and integration foundations

Continue without waiting through coherent slices where possible:

- manual, interval and cron triggers;
- signed normalized webhooks;
- trigger deduplication;
- concurrency and missed-schedule policies;
- operator/dead-letter UI;
- MCP STDIO adapter;
- MCP Streamable HTTP adapter;
- artifacts and structured long-term memory;
- secure node/external-executor contract;
- mocked Codex and Claude Code adapters;
- shared gateway message contract;
- safe package builder lifecycle;
- sanitized legacy importer contract.

## Critical requirements

- Keep all current tests passing.
- Add real HTTP/browser tests for the UI contracts.
- Preserve real Anthropic and OpenAI-compatible behavior.
- Never commit secrets.
- Never claim deployment.
- Do not create empty placeholders and mark them complete.
- Continue independently and checkpoint after each coherent milestone.
- Produce exact tree/patch/manifest artifacts after M1.6, after the durable worker/child-run layer, and after the workflow layer.
- Do not wait for confirmation between milestones.

Begin now with a factual baseline and M1.6. Your first user-facing completion response must include downloadable M1.6 artifacts, exact test results, commit SHAs, remaining gaps, and no deployment claims.
