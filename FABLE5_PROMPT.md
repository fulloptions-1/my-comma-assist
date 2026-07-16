# Start Atlas M1.6 and M2 from the current deployed source

Use public repository `fulloptions-1/my-comma-assist` and branch `atlas-fable-m2-current`.

Do not use `master`, `atlas-claude-review`, or the inherited root source as the product baseline.

1. Read root `CLAUDE.md` and `SNAPSHOT_INSTRUCTIONS.md`.
2. Use the verified plain-text source under `snapshot/tree/`.
3. Use `snapshot/MANIFEST.sha256` to verify the reconstructed local workspace.
4. Reconstruct a fresh local directory named `atlas-current` using the efficient delta procedure in `SNAPSHOT_INSTRUCTIONS.md` when the intact M1.5 workspace is available; otherwise fetch the plain files from `snapshot/tree/`.
5. Work only inside `atlas-current`.
6. Read `atlas-current/CLAUDE.md`, `atlas-current/FABLE5_PROMPT.md`, every relevant source and test file, `docs/LIVE_PRODUCT_GAPS.md`, and `docs/NEXT_ACCEPTANCE_MATRIX.md`.
7. Establish the exact baseline by running all locally possible tests and checks.
8. Execute the reconstructed tree's `FABLE5_PROMPT.md` completely.

The first required milestone is **M1.6**, which must turn the current working iPhone product into a clear, usable interface with target/model selection, capability library, guided provider setup, friendly failures, retry, proper Markdown, single response rendering, conversations, cost visibility, and PWA/mobile corrections.

After packaging M1.6, continue independently into **M2**: durable tasks/workers, child runs, parent wake-ups, cancellation, workflow DAGs, parallel branches, joins, questions, approvals, timers, events, retries, and failure policies.

Create checkpoint artifacts after every coherent milestone:

- tree ZIP;
- ordered Git-format patches ZIP;
- manifest JSON with hashes, commits, migrations, environment changes, exact tests and actual results.

Do not wait for user confirmation between milestones. Do not claim deployment. Do not commit secrets. Your first completion response must return downloadable M1.6 artifacts and exact verification results, not another plan alone.
