# Atlas — Current Production Snapshot Bootstrap

Work only with public repository `fulloptions-1/my-comma-assist`, branch `atlas-fable-m2-current`.

The authoritative current source is **not** the inherited root source tree. It is the verified archive:

`atlas-fable-m2-current-snapshot.zip`

SHA-256:

`31bab6e16af95b35c1ad73be9b5f72303a740e12740b2cc4aa2d5d4bcd67ba24`

Read `SNAPSHOT_INSTRUCTIONS.md`, fetch the archive through the GitHub connector, verify its checksum, and extract it into a fresh local directory named `atlas-current`. Work only inside that extracted directory.

The archive contains the actual deployed-source baseline after M1.5 and the live Anthropic tool-name hotfix. It also contains the authoritative current versions of:

- `CLAUDE.md`
- `FABLE5_PROMPT.md`
- `docs/LIVE_PRODUCT_GAPS.md`
- `docs/NEXT_ACCEPTANCE_MATRIX.md`
- all production source, tests, package definitions, scripts, and deployment docs

After extraction, read those files and every tracked source/test file before editing.

## Current live state

Atlas is running on Railway and works from iPhone Safari with:

- persistent `/data` storage;
- owner authentication;
- encrypted provider credentials;
- funded and tested Anthropic access;
- real Anthropic assistant responses;
- direct Car Maintenance behavior;
- durable questions, approvals, events, and restart recovery.

The current product is a strong M1.5 foundation, not the complete system. The next work is M1.6 product/UX correction followed by M2 durable child runs, workers, workflows, and automation foundations.

## Non-negotiable rules

- Models propose decisions; deterministic code owns execution and safety.
- Explicit targets bypass Concierge.
- Do not restore the old Router-agent chain.
- Preserve exact approvals, idempotency, version/profile pinning, permissions, budgets, and restart recovery.
- Do not commit secrets, user data, access tokens, API keys, Railway variables, or logs.
- Do not claim pushed, merged, deployed, remotely verified, or live.
- Produce auditable tree/patch/manifest checkpoints for ChatGPT.
- Continue independently through coherent milestones; checkpoint before moving onward.

Start with the extracted archive's `FABLE5_PROMPT.md` and follow it completely.
