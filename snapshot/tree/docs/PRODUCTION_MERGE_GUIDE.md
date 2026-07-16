# Merging this release into the private production repository

The private repo (`Chatgpt-extension-Version-44`) carries a NEWER chat UI than the
public review snapshot. This release deliberately splits backend from interface so you
can take the backend without regressing that UI.

## What to merge
- `atlas/*.py` — the entire backend (kernel + HTTP layer). Required.
- `packages/` — only if unchanged privately (it is unchanged in this release).
- `railway.toml`, `Dockerfile` — healthcheck now `/ready`; image defaults `ATLAS_ENV=production`.
- `docs/` — additive.
- `tests/` — required; the suite is the acceptance evidence.

## What NOT to overwrite
- **Your private `atlas/static/` (or wherever the newer chat UI lives).** The static
  files in this release are a functional reference implementation, not an upgrade over
  a newer UI. Keep yours; it only needs the endpoint contract below.

## Endpoint contract the UI can rely on
- `GET /auth/status` → `{mode: "open"|"secured", authenticated: bool}`
- `POST /auth/login` `{secret}` → sets `atlas_session` HttpOnly cookie (SameSite=Strict, Secure in prod)
- `POST /auth/logout`; bearer `Authorization: Bearer <token>` still works everywhere
- `GET /api/agents` → `[{id, name, description, handler}]`
- `GET /api/runs`; `POST /api/runs` `{target_id, message}`; `GET /api/runs/{id}`
  (detail includes `events[]` and pending `interaction`); `POST /api/runs/{id}/answer`
  `{interaction_id, answer}` — approvals answer `true`/`false`, and
  `interaction.payload.proposed_action` is the exact payload the approval hash binds
- `GET/PUT /api/settings/providers[/anthropic|/openai_compat]` (keys write-only, status redacted)
- `GET/PUT/DELETE /api/settings/profiles[/{id}]`
- `GET /health` (liveness, includes `agents`), `GET /ready` (readiness)
- Mutations from cookie sessions must be same-origin (Origin/Host guard); browser
  clients need `credentials: 'same-origin'` and no cross-site calls.

## Apply order
```
unzip atlas-v1-M1-patches.zip -d /tmp/m1 && git checkout -b atlas-m1 main
git am /tmp/m1/*.patch      # applies onto the reviewed baseline lineage
```
If your main has diverged in `atlas/app.py`, prefer taking this release's file wholesale
and re-pointing static serving at your UI directory — the app no longer contains UI.


## M1.2 notes
- Backend-only merge as always: the private repository's chat-style UI is
  canonical; take `atlas/` (except `atlas/static/`), `packages/`, `scripts/`,
  `tests/`, `docs/`. The reference static UI changed only additively
  (effective-agent label).
- New env var: `ATLAS_ACK_EPHEMERAL_STORAGE=1` (explicit opt-out when no
  volume is mounted). `/ready` now includes a `storage` field — surface it in
  diagnostics.
- New/changed durable event types: `agent.provider_attempt`,
  `run.continuation_resumed`, `tool.abandoned` (now with `reason`),
  `user_input.resolved` may carry `[redacted]` for free-text answers.
- Run detail API adds `effective_agent_id`; `run.target_id` remains the entry
  target.
- Migration 0004 (`tool_executions.owner`) applies automatically on boot.


## Connector / SSRF policy (v1, M1.3 §8)
Atlas v1 is single-tenant and owner-controlled: the only party who can
configure outbound connector URLs is the owner. The enforced policy:
https is REQUIRED in production; http is permitted only for loopback hosts
(localhost, 127.0.0.1, ::1) and only outside production; embedded
credentials in URLs are rejected everywhere; URLs are normalized before
storage. When multi-connector support lands, this section must grow an
explicit host allowlist and egress documentation BEFORE any
non-owner-configurable fetching is added.

## M1.3 merge notes
- request models are module-scope in atlas/app.py — keep them there; the
  OpenAPI pin test enforces it
- new env var: ATLAS_VOLUME_PATH (default /data); /ready now returns
  storage_evidence
- Runtime accepts secret_key; if the private repo constructs Runtime
  directly, pass its configured key
