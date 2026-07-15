# FABLE5 Implementation Plan

## Highest-priority implementation

1. Preserve direct assistant invocation by keeping provider dispatch in `assistantd`, not an old Router-agent chain.
2. Introduce a deterministic `RunKernel` responsible for assistant state, side-effect gating, retries, recovery accounting, payload budgets, and dispatch decisions.
3. Keep provider-specific behavior behind adapters (`AssistantProvider`, `HttpProvider`, `DisabledProvider`, `ProviderFactory`).
4. Add safe serialization that removes `logMonoTime`, replaces binary fields with size markers, and preserves service identity.
5. Add environment-driven configuration for endpoint, provider, enabled state, service narrowing, update timeouts, loop sleep, retries, and payload budget.
6. Add focused tests for every critical behavior implemented in this pass.
7. Update the acceptance matrix with evidence and honest statuses.

## Deferred implementation

- Real account-backed homelab credentials and endpoint validation.
- UI affordances for assistant approvals.
- Device/HIL validation of complete car-maintenance flows.
- Additional provider adapters beyond the lean deployable HTTP adapter.
