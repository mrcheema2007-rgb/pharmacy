# Pharmacy Cloud Sync Service

A small, separate FastAPI service that acts as a **generic cloud mirror**
for the pharmacy's Access database. It does not talk to Access or pyodbc
at all - Railway (or any cloud host) has no way to reach a Windows PC's
local `.accdb` file, and shouldn't try to. Instead:

- The **PC sync agent** (see `../sync_agent`) is the only thing with
  direct pyodbc access to `ph.accdb`. It pushes/pulls deltas to/from this
  service - nothing else runs on the pharmacy PC.
- The **cloud pharmacy_access_api** (`../pharmacy_access_api`) is a second
  Railway service in this same project, sharing this Postgres database. It
  reads/writes the typed tables this service materializes (see below)
  rather than the JSONB mirror directly, so it can run real SQL.
- The **Flutter mobile app** talks to that cloud API for everything
  (dashboard, sales, inventory, reports) - see `../pharmacy_manager_flutter`.
  It also talks to *this* service directly for its own offline-write
  queue's sync status, when it has cached writes made while genuinely
  offline.
- **This service is the mailbox, not the source of truth.** The pharmacy's
  Access database remains authoritative. This service just holds a mirror
  so multiple devices can exchange changes when they're not all on the
  same network at the same time.

## Why one generic table instead of 38, for the sync protocol itself

Rather than reimplementing 38 typed Postgres tables that would need to be
kept in lockstep with the Access schema just to run the push/pull
protocol, this service stores every synced row in one generic table:

```sql
synced_records(table_name, record_key, data JSONB, row_hash, device_id, updated_at, deleted)
```

keyed by `(table_name, record_key)`, where `record_key` is the same
pipe-joined primary-key string `pharmacy_access_api` already uses in its
URLs (e.g. `"1"`, or `"BATCH01|1001|2026-12-31"` for `BatchInfo`'s
composite key). This table is created automatically on first startup -
there's nothing to migrate by hand.

**There are also 38 real typed tables** (`app/materialize.py`), one per
Access table, kept in step with `synced_records` on every push/delete.
These exist for a different reason: they let the cloud
`pharmacy_access_api` service (see
`../pharmacy_access_api/README.md`) run genuine SQL - joins, aggregates,
sorting - against real columns, since you can't do that against an opaque
JSONB blob. `synced_records` remains the single source of truth for sync
bookkeeping (row hashes, conflict-free upserts, soft deletes); the typed
tables are a derived, disposable read/write mirror of it, rebuilt from
scratch (`CREATE TABLE IF NOT EXISTS`) on every startup if missing.

## Important limitations - please read before relying on this

- **Auto-increment primary keys don't round-trip safely.** If the mobile
  app creates a *new* record (say, a new `Customer`) while it's remote and
  pushes it to the cloud, and the PC agent later pulls it down and asks
  Access to create it, **Access will assign its own new AutoNumber ID** -
  it can't be forced to reuse the ID the mobile app/cloud used. That new
  Access record's key will no longer match `record_key` in the cloud
  mirror, breaking the link for future syncs of that specific record.
  Practical mitigations: prefer creating brand-new records on the PC
  (where they get a "real" ID immediately), or restrict remote creation to
  tables where you assign PKs yourself rather than AutoNumber, or accept
  that remote-created records need a one-time manual reconciliation. This
  is a fundamental distributed-ID problem, not something papered over here.
- **Conflict resolution is last-write-wins by `updated_at`.** If two
  devices edit the same record while both are offline from each other,
  whichever push reaches this service last simply overwrites the mirror -
  there's no merge and no conflict UI. For a small pharmacy team this is
  usually fine in practice; it's not appropriate for high-contention data.
- **This service has no idea what "valid" data looks like** for a given
  Access table (types, required fields, max lengths) - all of that
  validation still happens elsewhere, either in the PC sync agent when it
  applies a pulled record to Access, or in the cloud `pharmacy_access_api`
  service for writes made there. This service will happily store whatever
  JSON it's given.
- **Deleted rows are soft-deleted, never purged**, so pull-since-timestamp
  keeps working correctly. Add a periodic cleanup job yourself if you want
  to hard-delete very old soft-deleted rows.

## API

All `/sync/*` routes require an `X-Sync-Api-Key` header matching the
`SYNC_API_KEY` you configure (see below). This is a shared device secret,
**not** a user-login system - there's still no concept of individual users
anywhere in this project, matching the source Access database.

| Method | Path           | Purpose                                                |
|--------|----------------|---------------------------------------------------------|
| POST   | `/sync/push`   | Upsert changed rows (and/or deleted keys) from a device |
| GET    | `/sync/pull`   | Get rows changed since a timestamp, excluding your own  |
| GET    | `/sync/status` | Per-table record counts and last-updated timestamps     |
| GET    | `/health`      | Liveness check (no API key required)                    |
| GET    | `/health/db`   | Postgres connectivity check (no API key required)       |

Interactive docs at `/docs` once deployed.

---

## Deploying to Railway

### 1. Create the Railway project
1. Go to [railway.app](https://railway.app) and sign in.
2. **New Project** → **Deploy from GitHub repo** (push this
   `cloud_sync_service` folder to its own GitHub repo first - Railway
   deploys from a repo, not a local zip), or **Empty Project** if you'd
   rather deploy with the Railway CLI (`railway up` from inside this
   folder, after `railway init`).

### 2. Add a PostgreSQL database
1. Inside your new Railway project, click **+ New** → **Database** →
   **Add PostgreSQL**.
2. Railway provisions a Postgres instance and automatically creates a
   `DATABASE_URL` variable. **You don't need to create it yourself.**
3. Click the new Postgres service, open its **Variables** tab, and note
   that `DATABASE_URL` lives there - Railway will let you reference it
   from your app service in the next step.

### 3. Connect the app service to the database
1. Click on your app service (the one deploying this FastAPI code) →
   **Variables** tab.
2. Add a variable reference to the Postgres service's `DATABASE_URL`:
   click **+ New Variable** → **Add Reference** → select the Postgres
   service → `DATABASE_URL`. (Railway's UI calls this "referencing" a
   variable from another service - it keeps them in sync automatically if
   the database ever moves.)
3. Add one more variable yourself: `SYNC_API_KEY` = a long random string
   you generate (e.g. `openssl rand -hex 32` or any password generator).
   Use this exact value in the PC sync agent's `.env` and in the Flutter
   app's cloud sync settings.

### 4. Deploy
1. Railway auto-detects this as a Python app via Nixpacks (it reads
   `requirements.txt`) and uses the `startCommand` from `railway.json`
   (falls back to `Procfile` if you prefer that instead - both are
   included, `railway.json` takes priority).
2. Push to your connected GitHub branch, or run `railway up` - Railway
   builds and deploys automatically.
3. Once deployed, Railway gives your service a public URL like
   `https://your-service-name.up.railway.app`. Find it under the app
   service's **Settings** → **Networking** → **Generate Domain** if one
   wasn't created automatically.

### 5. Verify
```bash
curl https://your-service-name.up.railway.app/health
# {"status":"ok"}

curl https://your-service-name.up.railway.app/health/db
# {"database":"reachable"}

curl -X POST https://your-service-name.up.railway.app/sync/push \
  -H "X-Sync-Api-Key: <your SYNC_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"test","table_name":"Customer","records":[{"record_key":"1","data":{"Customer_Name":"Test"},"row_hash":"abc"}]}'
# {"table_name":"Customer","upserted":1,"deleted":0}
```

### 6. Point your devices at it
- **PC sync agent** (`../sync_agent/.env`): set `CLOUD_API_URL` to your
  Railway URL and `SYNC_API_KEY` to the same value you set in Railway.
- **Flutter app** (Settings screen, or `--dart-define` at build time): set
  the cloud sync base URL and API key the same way - see
  `../pharmacy_manager_flutter/README.md`.

### Local testing without Railway
```bash
cd cloud_sync_service
cp .env.example .env
# edit .env: point DATABASE_URL at a local Postgres (docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16 works fine)
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
