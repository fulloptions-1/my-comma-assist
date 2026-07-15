# FABLE5 Acceptance Matrix

| Area | Status | Evidence |
| --- | --- | --- |
| Direct agent invocation preserved | Complete | `RunKernel.dispatch()` calls the configured provider directly; no Router-agent chain was added. |
| Provider-specific behavior isolated | Complete | Provider-specific HTTP behavior lives in `HttpProvider`; construction is centralized in `ProviderFactory`. |
| Deterministic run kernel owns state and side effects | Complete | `RunKernel` owns live context, sent/failure/drop counts, payload budgets, retries, and dispatch decisions. |
| Safe telemetry serialization | Complete | `extract_message_data()` removes `logMonoTime`, scrubs bytes, and preserves `_service`. |
| Retry and recovery accounting | Complete | Dispatch retries provider failures and records failure state before recovery. |
| Payload budget enforcement | Complete | Oversized payloads are dropped before provider side effects. |
| Automated critical behavior tests | Complete | `selfdrive/assistantd/tests/test_assistantd.py` covers serialization, provider dispatch, retries, budgets, disabled mode, and service filtering. |
| Complete car-maintenance flow | Blocked | No app/flow files or requested briefing docs were present in the checkout; no secrets or external accounts were accessed. |
| Account-backed integrations | Blocked | Requires user-controlled endpoint/account configuration. |
| Device/HIL verification | Deferred | Requires hardware and live vehicle/simulation environment. |
