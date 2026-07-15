# FABLE5 Findings

## Baseline status

- The requested branch `atlas-claude-review` was not available from the local clone, and fetching `https://github.com/fulloptions-1/my-comma-assist.git` failed with `CONNECT tunnel failed, response 403`. Work was kept on a local `atlas-claude-review` branch created from the provided checkout.
- The requested briefing files (`CLAUDE.md`, `FABLE5_PROMPT.md`, and the listed FABLE5 docs) were absent from the checkout at the start of the review.
- The existing assistant implementation was a monolithic loop in `selfdrive/assistantd/assistantd.py` that subscribed to safe cereal services and held live context, but the homelab integration, deterministic run kernel, provider adapter boundary, retry handling, payload budgeting, and automated tests were missing.
- The bundled `.venv` did not include `pytest` or `pip`, so the initial test baseline failed before dependencies were installed.

## Functional gaps found

1. No provider adapter boundary existed; network behavior would have been added directly in the daemon loop.
2. Direct agent/provider invocation was not implemented or testable.
3. The daemon had no deterministic unit-testable kernel for state, retries, recovery, budgets, or side-effect gating.
4. Payloads could include binary fields and unbounded context size.
5. Parse failures returned an opaque error and dropped service identity.
6. Service selection could not be narrowed for focused deployments or tests.
7. No automated tests covered the assistant daemon.

## Verification findings

- A full openpilot application start and complete car-maintenance flow could not be truthfully verified in this environment because the requested app/flow documentation was absent and the checkout is an openpilot/sunnypilot tree with only the assistant daemon addition visible.
- Assistant daemon behavior is now verified with focused automated tests covering serialization, provider invocation, retries, payload budgets, disabled mode, and service filtering.
