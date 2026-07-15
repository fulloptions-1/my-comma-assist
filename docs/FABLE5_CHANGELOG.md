# FABLE5 Changelog

## Completed in this review

- Reworked `assistantd` around a deterministic run kernel.
- Added provider adapter interfaces and a lean HTTP provider.
- Added disabled mode to prevent accidental network side effects unless explicitly enabled.
- Added configurable service selection and payload budgets.
- Added safe JSON serialization for assistant context payloads.
- Added retry/recovery accounting for provider dispatch.
- Added focused automated tests for assistant daemon behavior.
- Added findings, implementation plan, and acceptance matrix evidence.

## Missing engineering

- Full app-level car-maintenance flow implementation was not discoverable in this checkout.
- UI approval surfaces are not implemented.
- Durable persistence across daemon restarts is not implemented.

## Account-blocked integrations

- Real homelab endpoint verification requires user-owned endpoint configuration and any required account/network access.

## Deferred work

- WebSocket or model-provider-specific adapters.
- Hardware-in-the-loop validation on a comma device.
- Broader integration tests against live cereal streams.
