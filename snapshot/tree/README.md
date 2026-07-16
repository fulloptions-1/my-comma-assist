# Atlas Engine — Public Claude Review Snapshot

This branch is a public, secret-free snapshot of the currently deployed Atlas vertical slice plus the full review brief for Claude Fable 5 / Claude Code.

**Review branch:** `atlas-claude-review`

Start with:

1. `CLAUDE.md`
2. `FABLE5_PROMPT.md`
3. `docs/FABLE5_REVIEW_BRIEF.md`
4. `docs/LEGACY_SWARM_CONTEXT.md`
5. `docs/CURRENT_ATLAS_STATE.md`
6. `docs/FABLE5_ACCEPTANCE_MATRIX.md`
7. Every source and test file in this branch

This branch is intentionally isolated from the private production repository. It contains no API keys, Railway secrets, user data, or historical logs.

The live product remains in the private `Chatgpt-extension-Version-44` repository. Review changes should be developed here first and then applied to the private production repository only after human review.

## Fable 5 review (2026-07)

This branch carries an implementation review by Claude Fable 5. Start with
`docs/FABLE5_FINDINGS.md` (baseline + defects), `docs/FABLE5_IMPLEMENTATION_PLAN.md`
(what was built and what is deliberately deferred), and `docs/FABLE5_CHANGELOG.md`
(per-commit changes, verification commands, acceptance-matrix statuses).
