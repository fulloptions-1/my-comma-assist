# Deferred hardening register (M1.4 §9)

Items ChatGPT's M1.3 audit listed for honest tracking. Two were fixed in
M1.4 (provider response size cap; provider tool-argument JSON constants —
see the §5 providers commit). The rest are explicitly DEFERRED with the
acceptance test each must ship with. This register must shrink, never
silently disappear.

| Item | Current behavior | Acceptance test when fixed |
|---|---|---|
| Deep type-validation of LLM-specific YAML fields at package load | registry validates structure/ids/handlers/tools; llm numeric fields are read via pick() with runtime coercion, so a string max_tokens in YAML fails at run time, not load time | loading a package whose agent declares max_tokens: "x" raises PackageError naming file+field at REGISTRY LOAD |
| Side-effect resource-lock lease renewal | the 30s lock lease is taken once; a longer side effect holds a stale-looking lock (single-process: harmless; boot reclaim owns recovery) | a handler running 2x the lease under an injected clock renews and completes without a second claimant stealing |
| Stuck read-only worker threads at shutdown | caller returns at the deadline; a permanently-stuck thread is non-daemon and can delay interpreter exit | gateway-owned single pool with daemon threads (or joined-with-timeout shutdown hook); test: interpreter exits < 2s with a stuck probe thread |
| Provider error details entering run output/events | failure paths store str(exc); adapter messages include status + truncated wire detail; probe detail capped at 200 chars | run.failed payloads for provider errors carry category + capped detail; a 10KB upstream error body never appears verbatim in events |
| Raw run context in the reference UI | the reference UI shows context under a collapsed details block | reference UI gates raw context behind a diagnostics toggle; contract test asserts the default thread payload excludes context |
| Private production chat UI | reference UI is NOT the production UI; merge guide mandates backend-only merges | integration checklist item in the private repo (outside this tree's control); guide re-asserts it every release |
