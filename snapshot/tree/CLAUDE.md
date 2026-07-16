# Atlas — Claude Fable 5 Current-Production Review and Build Instructions

Work only on the branch `atlas-fable-m2-current` in the public repository `fulloptions-1/my-comma-assist`.

This branch is a secret-free snapshot of the **currently deployed private Atlas source** after M1.5 and the provider tool-name hotfix. It is the authoritative engineering baseline for the next build. Ignore `master`, `atlas-claude-review`, and all earlier public snapshots.

## Current verified production status

Atlas is live on Railway and usable from iPhone Safari. The user has:

- a persistent `/data` Railway volume;
- production authentication and encrypted provider credentials;
- a working Anthropic key and funded account;
- a working active Anthropic profile;
- successful live Anthropic responses after the provider-safe tool-name hotfix;
- passing private-repository CI for the current source.

Do not claim access to Railway secrets or the private repository. Do not attempt deployment. Produce auditable release artifacts for ChatGPT to inspect and merge.

## Governing architecture rule

> Models propose decisions. Deterministic code owns state, execution, permissions, retries, scheduling, recovery, budgets, approvals, and side effects.

Preserve:

- direct target invocation;
- Concierge only when no target is selected;
- durable event history and projections;
- exact approval binding;
- tool idempotency and permissions;
- pinned agent/model settings for in-flight runs;
- one deployable service and durable database until scale proves otherwise;
- strict package validation;
- provider adapters behind one contract.

Do not reintroduce a default Router-agent chain, global active agent, board.md as truth, untyped model commands, Kubernetes, a message broker, or speculative microservices.

## Required reading

Read every tracked file before changing code, then read:

1. `FABLE5_PROMPT.md`
2. `docs/LIVE_PRODUCT_GAPS.md`
3. `docs/NEXT_ACCEPTANCE_MATRIX.md`
4. current tests and package definitions
5. architecture/findings/changelog documents already in `docs/`

Cross-check every claim against the code.

## Mandatory baseline

Before editing:

1. Install exact pinned dependencies when the environment permits.
2. Run the entire test suite.
3. Run compileall and JavaScript syntax checks.
4. Start the real FastAPI app or use TestClient.
5. Exercise direct Assistant, Auto/Concierge, Car Maintenance, question, approval, settings, and provider profile flows.
6. Record the exact baseline in `docs/NEXT_FINDINGS.md`.

## Work behavior

- Do not stop at analysis; implement and test.
- Continue independently through M1.6 and as much of M2 as can be completed coherently.
- Checkpoint after every coherent milestone without waiting for user confirmation.
- Never call local work pushed, merged, deployed, remotely verified, or live.
- Never commit secrets, real API keys, access tokens, user data, Railway configuration, or historical logs.
- Never report stubs as finished capabilities.
- Use small reviewable commits.
- Keep existing API behavior backward compatible unless a migration and compatibility path are included.

## Required checkpoint artifacts

After each coherent milestone create:

- `atlas-next-M<N>-tree.zip`
- `atlas-next-M<N>-patches.zip`
- `atlas-next-M<N>-manifest.json`

Also update:

- `docs/HANDOFF_TO_CHATGPT.md`
- `docs/NEXT_FINDINGS.md`
- `docs/NEXT_IMPLEMENTATION_PLAN.md`
- `docs/NEXT_CHANGELOG.md`
- `docs/NEXT_ACCEPTANCE_MATRIX.md`
- `docs/VERIFICATION.md`
- `docs/RAILWAY_DEPLOYMENT.md`

The manifest must contain exact commit SHAs, file changes, hashes, migrations, environment changes, exact tests executed, actual results, known limitations, deployment procedure, and rollback procedure.
