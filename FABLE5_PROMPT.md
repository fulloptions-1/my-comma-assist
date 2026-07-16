# Start Atlas M1.6 and M2 from the current deployed source

Use public repository `fulloptions-1/my-comma-assist` and branch `atlas-fable-m2-current`.

Do not use `master`, `atlas-claude-review`, or the inherited root source as the product baseline.

1. Read root `CLAUDE.md` and `SNAPSHOT_INSTRUCTIONS.md`.
2. Fetch `atlas-fable-m2-current-snapshot.zip` through the GitHub connector.
3. Verify SHA-256:
   `31bab6e16af95b35c1ad73be9b5f72303a740e12740b2cc4aa2d5d4bcd67ba24`
4. Extract it into a fresh local directory `atlas-current`.
5. Work only inside `atlas-current`.
6. Read the extracted `CLAUDE.md`, extracted `FABLE5_PROMPT.md`, every source and test file, `docs/LIVE_PRODUCT_GAPS.md`, and `docs/NEXT_ACCEPTANCE_MATRIX.md`.
7. Establish the exact baseline by running all locally possible tests and checks.
8. Execute the extracted prompt completely.

The first required milestone is **M1.6**, which must turn the current working iPhone product into a clear, usable interface with target/model selection, capability library, guided provider setup, friendly failures, retry, proper Markdown, single response rendering, conversations, cost visibility, and PWA/mobile corrections.

After packaging M1.6, continue independently into **M2**: durable tasks/workers, child runs, parent wake-ups, cancellation, workflow DAGs, parallel branches, joins, questions, approvals, timers, events, retries, and failure policies.

Create checkpoint artifacts after every coherent milestone:

- tree ZIP;
- ordered Git-format patches ZIP;
- manifest JSON with hashes, commits, migrations, environment changes, exact tests and actual results.

Do not wait for user confirmation between milestones. Do not claim deployment. Do not commit secrets. Your first completion response must return downloadable M1.6 artifacts and exact verification results, not another plan alone.
