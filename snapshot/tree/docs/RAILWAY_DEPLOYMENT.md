# Railway deployment

## Required configuration (production)

| Variable | Purpose | Notes |
|---|---|---|
| `ATLAS_ENV` | `production` enables strict preflight | Dockerfile default is `production`; set `development` locally |
| `ATLAS_ACCESS_TOKEN` | Owner secret for login + bearer API | Generate: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`. **Fatal if missing in production.** |
| `ATLAS_SECRET_KEY` | Fernet key for encrypted provider secrets | Generate: `python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`. **Fatal if missing in production.** |
| `ATLAS_DB_PATH` | SQLite path | Must be `/data/atlas.db` with a Railway **volume mounted at `/data`**, or state is lost on redeploy. |
| `ATLAS_ACK_EPHEMERAL_STORAGE` | Set `1` to knowingly run production on ephemeral storage | Only for throwaway environments. |
| `ATLAS_VOLUME_PATH` | Where the persistent volume is EXPECTED (default `/data`) | The database must live under this path; preflight verifies this exact root (or an ancestor of the db dir beneath it) is a real mount. |
| `ATLAS_RATE_LIMIT` | Mutations/minute/IP (default 30) | Optional |

Set both secrets in Railway service variables only — never in Git, prompts, or logs.

## Persistence — what preflight can and cannot detect

The Docker image creates `/data` itself, so the directory **always exists
and is always writable** — its presence proves nothing. What preflight CAN
check on Linux is whether the database directory is a real **mount point**:
a Railway volume mounted at `/data` is one; the image-created directory is
not. Preflight classifies storage as one of:

- `mounted` — a volume is attached; production boots normally.
- `ephemeral-acknowledged` — no mount, but `ATLAS_ACK_EPHEMERAL_STORAGE=1`
  was set; production boots, data is lost on redeploy, on purpose.
- `unconfirmed` — no mount and no acknowledgement; **production refuses to
  boot** with instructions in the log. This includes a database placed
  OUTSIDE `ATLAS_VOLUME_PATH` — a mounted volume elsewhere proves nothing
  about where Atlas actually writes.

The container root filesystem `/` is **never** counted as evidence (it is
always a mount point; treating it as one would bless every path — the M1.2
defect). `/ready` returns the full detection evidence:
`storage_evidence: {volume_root, db_dir, mounted_at, reason}`.

Setup: Railway → your service → **Volumes** → add volume, mount path
`/data`. Verify after deploy:

```
curl -s https://<app>/ready | python3 -m json.tool   # "storage": "mounted"
railway ssh -- python3 -c "import os; print(os.path.ismount('/data'))"
```

Preflight cannot detect a volume that is attached but *empty vs. populated*,
mounted at the wrong path, or a network filesystem's durability guarantees —
`/ready`'s `storage` field reports mount status only.

## Health vs readiness
- `GET /health` — liveness: process is up (always 200 once imported).
- `GET /ready` — readiness: DB answers, `schema_version == schema_latest`, agents loaded.
  Returns 503 with a reason until true. `railway.toml` points the platform healthcheck here.

## Migrations
Applied automatically at startup (`atlas/migrations.py`): each numbered migration and its
ledger row commit in one transaction or roll back together with a loud `MigrationError`
(the app then fails readiness instead of running on a half-upgraded schema). Existing
pre-mechanism databases are adopted in place; data is preserved (tested).

## Backup and restore

The image is `python:3.12-slim`, which does **not** include the `sqlite3`
command-line tool — use the bundled Python script (safe against the live
WAL-mode database, verifies the copy):

```
railway ssh -- python scripts/backup_db.py /data/atlas.db \
    "/data/backups/atlas-$(date +%Y%m%d-%H%M).db"
```

Restore: stop the service, copy the chosen backup over `/data/atlas.db`,
delete any stale `atlas.db-wal` / `atlas.db-shm` files, start the service.
Migrations re-verify the schema on boot.

## Rollback
1. Redeploy the previous image (Railway → Deployments → Redeploy).
2. If a migration already ran and the old code can't read the new schema, restore:
   stop traffic, `cp /data/backup-<stamp>.db /data/atlas.db`, redeploy old image.
   Migrations 0001–0003 are additive, so old code tolerates them; treat restores as
   the escape hatch for future destructive migrations.

## Graceful shutdown
uvicorn drains in-flight requests on SIGTERM; the app's shutdown hook then runs a
best-effort `wal_checkpoint(TRUNCATE)` so the container leaves a compact, consistent DB.
