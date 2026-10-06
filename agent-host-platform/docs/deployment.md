# Production deployment guide — Universal AGT

This guide takes you from a fresh checkout to a working production system:
Supabase (or any Postgres 14+) → control-plane API behind HTTPS → one
persistent host with the worker → two agents (Muse + Instinct) → your first
deployment → optional public ingress. Everything here is verified against
the implementation; where a value is a secret, the guide uses a
`<PLACEHOLDER>` — **never commit real credentials**.

> Agent-neutral by design: the control plane has no agent-type
> special-casing. Muse and Instinct below are just two agent rows with
> different permission sets.

## 0. What you need

- A Supabase project (or any Postgres 14+ you operate). You need a
  connection string and, once, a **superuser** connection for migration
  `001` (installs the `pgcrypto` extension). Supabase's SQL
  editor runs as superuser — fine. The API's startup preflight
  (`checkMigrationPrivileges`) detects the missing privilege and tells you
  exactly which migration needs it and the safe path (SQL editor), instead
  of failing mid-migration.
- A Linux VM/container for the control plane (Node 20+, outbound HTTPS).
- One persistent Linux host (x86_64 or arm64) with Docker, for the worker.
- A domain name you control (only needed for public ingress / HTTPS).
- `openssl` for generating secrets.

## 1. Database: Supabase setup + migrations in order

1. Create a Supabase project; note the **connection string**. For the
   control-plane API, use the **direct connection** (port `5432`) or a
   **session-mode** pooler URI — NOT the transaction-mode pooler: the API
   holds a persistent `LISTEN uag_events` connection for the SSE event
   bus, and transaction-mode poolers (Supavisor) do not support
   `LISTEN`/`NOTIFY`. The bus degrades gracefully (a 5s backstop poll
   keeps events flowing when `LISTEN` is unavailable), but live SSE
   latency stretches to ~5s. The direct connection also avoids surprises
   with the startup migration runner, which holds one connection per
   migration.
2. Apply the migrations **in order** — either let the API do it (it runs
   `runMigrations` at every startup) or apply explicitly:

```bash
cd agent-host-platform/control-plane/api
npm ci && npm run build
DATABASE_URL="<your-supabase-connection-string>" npm run migrate
```

The files: `agent-host-platform/database/migrations/`:

| # | File | What it does |
|---|---|---|
| 001 | `001_initial.sql` | Core tables: agents, hosts, projects, artifacts, deployments, tasks, secrets, events, port_allocations. Installs the `pgcrypto` extension — **needs superuser** on a fresh database (enable in the Supabase dashboard → Database → Extensions, or run `create extension "pgcrypto";` once as superuser). |
| 002 | `002_artifact_status.sql` | Artifact upload status lifecycle |
| 003 | `003_events_notify.sql` | `pg LISTEN/NOTIFY` plumbing for the SSE event bus |
| 004 | `004_phase3_reliability.sql` | Claim leases, retry bookkeeping, port registry, `draining` host state |
| 005 | `005_events_truncate_block.sql` | `REVOKE TRUNCATE ON events FROM PUBLIC` — stops non-owner roles truncating the append-only journal (no superuser needed; full protection needs separate table ownership, see `docs/security.md`) |
| 006 | `006_token_rotation_grace.sql` | Host token rotation grace window: `previous_token_hash` + `previous_token_expires_at` on `hosts` (no privilege issues) |
| 007 | `007_domains_lifecycle.sql` | Domains become a first-class `domains` table (`requested → configuring → active → failed`, `degraded` as a live-but-unverified state, `removing → removed`); backfills from the legacy `deployments.domains` JSONB mirror (no privilege issues) |
| 008 | `008_task_queue_hardening.sql` | `domains.status` CHECK recreated to allow `'degraded'` (007's constraint omitted it while the reconciler transitions to it — real PostgreSQL would 500; pg-mem doesn't enforce CHECKs so suites stayed green); `domains.idempotency_key` unique; `tasks.type` 18-type CHECK + `attempts >= 0` + `max_attempts >= 1`; `deployments.health_status` CHECK; three hot-path indexes (`idx_tasks_claimable`, `idx_tasks_claimed_by`, `idx_tasks_payload_deployment_id`) |
| 009 | `009_heartbeat_enrichment.sql` | `worker_draining`, `worker_status`, `ingress` (jsonb), `reported_host_name` columns for the enriched heartbeat (2026-10-06: `capabilities` already existed) |
| 010 | `010_registration_idempotency.sql` | Nullable `idempotency_key` on `hosts`/`agents` + partial unique indexes (registration replay) |
| 011 | `011_project_ownership_acls.sql` | Resource-level authorization: `projects.owner_agent_id` (backfilled from the legacy `owner` name; unresolvable owners recorded in `migration_reports`, never silently assigned), plus `project_members` and `agent_host_access` tables (no privilege issues) |
| 012 | `012_rollback_failed_status.sql` | Widens the `deployments.status` CHECK to admit `'rollback_failed'` (drop + re-add, safe no-op on re-run; no privilege issues) |
| 013 | `013_reserved_resources.sql` | Persistent resource reservations: `deployments.reserved_cpu` / `reserved_ram_mb`, snapshotted inside the same transaction that selects the host (no privilege issues) |
| 014 | `014_artifact_manifest.sql` | `artifacts.manifest` jsonb — the artifact's validated `agent.deploy.json`, stored at init; the scheduler reserves from it instead of `projects.configuration` (no privilege issues) |

Verify:

```sql
-- in the Supabase SQL editor
select * from schema_migrations order by name;  -- 001…014 present
```

### Privileged migrations — the safe path

Migration `001` (pgcrypto extension) needs superuser on a fresh database.
If the API connects as a non-superuser role, its startup
preflight refuses to run the migration and fails fast with a precise
error naming the migration — it never skips a security migration. The
safe path is always the same:

1. Copy the migration file's SQL into the **Supabase SQL editor** (runs
   as superuser) and run it there.
2. Mark it applied so the API doesn't retry it:
   `insert into schema_migrations (name) values ('005_events_truncate_block.sql');`
   (use the exact filename).
3. Restart the API; startup resumes normally.

`005` is database-global, not a schema object: `pg_dump` schema-only
dumps do not capture it, so re-apply it after any database restore.

### Migration runner notes

- Migrations are numbered `001`…`014`, no gaps (CI enforces this).
- A migration file may opt out of the runner's transaction wrapper with
  a marker line `-- migrate: no-transaction` (parsed by
  `src/db/migrate.ts` `requiresNoTransaction`) — for statements that
  cannot run inside a transaction block. No current migration uses it;
  the old `005` event-trigger revision needed it before PostgreSQL
  rejected the approach entirely (event triggers do not support
  `TRUNCATE` — verified on PG 16).
- `database/schema/schema.sql` is kept as the canonical superset of the
  chain (CI checks every table the migrations create exists there).

### Supabase project-level setup (what the dashboard/SQL editor must do once)

1. **`pgcrypto` extension** — allowlisted on Supabase; enable in
   Dashboard → Database → Extensions, or run
   `create extension "pgcrypto";` in the SQL editor. (Migration `001`
   does `CREATE EXTENSION IF NOT EXISTS "pgcrypto"` itself when the
   connection can — the API's startup preflight
   `checkMigrationPrivileges` fails fast with this exact path instead
   of a cryptic error when it cannot.)
2. **Role ownership.** Run the migrations as the `postgres` role (SQL
   editor). All other migrations (`002`–`014`) are plain DDL/DML that
   need only *table ownership*, not superuser — they run fine as any
   role that owns the tables, which is why the chain also works against
   a local Postgres when run as a non-superuser owner role. Keep the
   API's `DATABASE_URL` on the same role that owns the tables (the
   `postgres` role on Supabase), or `ALTER TABLE … OWNER TO` them all
   to a dedicated app role. Startup itself runs
   `CREATE TABLE IF NOT EXISTS schema_migrations`, so the runtime role
   needs `CREATE` on the `public` schema.
3. **No RLS needed.** The tables are not row-level-security gated; the
   API authorizes in application code. Supabase's `anon`/`service_role`
   keys are irrelevant here — connect the API as the owning role via
   the database connection string, not the API keys.
4. **Poole`r mode** — see §1: direct/session-mode for the API so
   `LISTEN uag_events` works; transaction mode only degrades SSE
   liveness to the 5s backstop poll.

## 2. Environment variables (control plane)

Copy `control-plane/api/.env.example` to `.env` and fill it in. Every
variable below is read by the code (defaults shown). The canonical
reference for every configuration name in the system — including the
`UAHT_*` installer-input → `WORKER_*` worker-runtime mapping — is
[`docs/CONFIG.md`](CONFIG.md): one name per concept.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — (required) | Postgres connection string (Supabase pooler URI works) |
| `PORT` | `3000` | API listen port |
| `DATA_ENCRYPTION_KEY` | — (required for secrets) | 64 hex chars (`openssl rand -hex 32`); AES-256-GCM key for project secrets. **Back it up — changing it invalidates stored secrets.** |
| `UAHT_PROVISIONING_TOKEN` | — | **The** provisioning token (one name, one token — see `docs/CONFIG.md`). Gates `POST /v1/agents/register` via the `X-Provisioning-Token` header (constant-time compare) **and** `POST /v1/hosts/register` via `Authorization: Bearer <token>` (hands-off first-boot provisioning; alternatively an agent key with `deploy` can register hosts). **Required to boot in production** — the startup validator (`src/lib/config.ts`) refuses to start without it. Unset in development = bootstrap mode: only the very first agent registration is open, then 403. |
| `ARTIFACT_MAX_BYTES` | `524288000` (500 MB) | Wire cap for artifact uploads (bytes). Enforced on `Content-Length` upfront, per-chunk while streaming, and on declared `size` at init → `413 payload_too_large`. |
| `ARTIFACT_DIR` | `./data/artifacts` | Where uploaded artifact bytes are stored (created if missing) |
| `LOG_DIR` | `./data/logs` | Where per-task worker log chunks are appended |
| `DASHBOARD_DIR` | `<repo>/dashboard` | Static files served at `/` |
| `RATE_LIMIT_AGENT_PER_MIN` | `120` | Per agent API key |
| `RATE_LIMIT_HOST_PER_MIN` | `600` | Per host token |
| `RATE_LIMIT_UNAUTH_PER_MIN` | `10` | Per IP, unauthenticated (registration, etc.) |
| `UAHT_ROTATE_RATE_PER_MIN` | `10` | Per credential, credential-rotation endpoints (`/agents/me/rotate`, `/hosts/:id/rotate-token`) |
| `HEARTBEAT_SWEEP_INTERVAL_S` | `30` | Stale-host sweeper cadence |
| `HEARTBEAT_DEGRADED_AFTER_S` | `90` | No heartbeat this long → `degraded` (`host.degraded`) |
| `HEARTBEAT_OFFLINE_AFTER_S` | `300` | No heartbeat this long → `offline` (`host.offline`). Operator-set `draining` is never auto-flipped. |
| `TASK_SWEEP_INTERVAL_S` | `60` | Stuck-task sweeper cadence |
| `TASK_CLAIM_LEASE_S` | `600` | Claim lease: a task in `claimed`/`running` with no progress past this is requeued (attempt counted) or failed when the retry budget is exhausted / the type isn't retry-safe. Refreshed on every progress report. |
| `PG_POOL_MAX` | `10` | Postgres pool size |
| `LOG_LEVEL` | `info` | — |
| `NODE_ENV` | — | `development` relaxes **only** the `UAHT_PROVISIONING_TOKEN` check (agent-registration bootstrap mode). Anything else (including unset) = strict production: the startup validator **refuses to boot** without `DATABASE_URL`, `DATA_ENCRYPTION_KEY` (64 hex), and `UAHT_PROVISIONING_TOKEN` |
| `CLOUDFLARE_API_TOKEN` | — | Scoped **Zone → DNS → Edit** on your zone (direct + tunnel modes) |
| `CLOUDFLARE_ZONE_ID` | — | Zone id for the DNS records |
| `PUBLIC_INGRESS_HOSTNAME` | — | Direct mode: CNAME target `hostname → PUBLIC_INGRESS_HOSTNAME` (your own ingress in front of the host) |
| `TUNNEL_INGRESS_HOSTNAME` | — | Tunnel mode: CNAME target, e.g. `<tunnel-id>.cfargotunnel.com`. When set, new domains default to `ingress: tunnel`; requesting tunnel mode without it → 422. |

Worker-side variables live in the host's `worker.env`
(see `host-worker/.env.example` and [`host-install.md`](host-install.md)):
`WORKER_CONTROL_PLANE_URL`, `WORKER_HOST_ID`, `WORKER_HOST_TOKEN`,
`WORKER_HOST_NAME`, `WORKER_POLL_WAIT` (25s, protocol max 30),
`WORKER_HEARTBEAT_INTERVAL` (30s), `WORKER_WORK_DIR` (`/opt/agent-host`),
`WORKER_APPS_DIR` (`/srv/agent-apps`),
`WORKER_CRASH_LOOP_THRESHOLD` (5) / `WORKER_CRASH_LOOP_WINDOW_S` (300s),
`DEPLOY_KEEP_GENERATIONS` (2), `WORKER_UPDATE_HEALTH_TIMEOUT_S` (120s),
and the optional ingress trio `WORKER_INGRESS_ENABLED` /
`WORKER_INGRESS_PROVIDER=cloudflare-tunnel` / `WORKER_TUNNEL_TOKEN`
(the installer accepts this last one as `UAHT_TUNNEL_TOKEN`; see
`docs/CONFIG.md` for the full operator-input → runtime mapping).

## 3. Control-plane deployment

```bash
cd agent-host-platform/control-plane/api
npm ci
npm run build
# .env holds DATABASE_URL, DATA_ENCRYPTION_KEY, UAHT_PROVISIONING_TOKEN, ...
npm start        # serves REST at /v1 and the dashboard at /
```

`npm start` rebuilds `dist/` from `src/` before running it, so it can never
execute a stale build — `dist/` is never committed to the repo. `npm run
prod` is an alias for `npm start`:

```bash
npm run prod     # same as npm start (builds from source, then serves)
```

`npm start` runs migrations automatically; `npm run migrate` does it
explicitly. Health check: `GET /v1/health` → `{ok: true, version, min_worker_version, time}`.

### systemd example

```ini
[Unit]
Description=Universal AGT control plane
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=uagt
WorkingDirectory=/opt/universal-agt/agent-host-platform/control-plane/api
EnvironmentFile=/etc/uagt/control-plane.env
# Build from source on every (re)start so stale compiled output can never
# execute. Deploy step must have run `npm ci` with devDependencies present
# (tsc lives there); `npm run build` is idempotent.
ExecStartPre=/usr/bin/npm run build
ExecStart=/usr/bin/npm start
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/universal-agt/agent-host-platform/control-plane/api/data

[Install]
WantedBy=multi-user.target
```

(`/etc/uagt/control-plane.env` holds the secrets from §2, mode `0600`.)

### Docker example

Built from source in a multi-stage image — the image never copies a
pre-built `dist/`; it compiles `src/` at build time, so the running code
always matches the checked-out source:

```dockerfile
# Stage 1: compile TypeScript from source
FROM node:20-slim AS build
WORKDIR /srv/api
COPY agent-host-platform/control-plane/api/package*.json ./
RUN npm ci
COPY agent-host-platform/control-plane/api/src ./src
COPY agent-host-platform/control-plane/api/tsconfig.json ./
RUN npm run build && npm prune --omit=dev

# Stage 2: minimal runtime (compiled output + prod deps only)
FROM node:20-slim
WORKDIR /srv/api
COPY --from=build /srv/api/package.json ./
COPY --from=build /srv/api/node_modules ./node_modules
COPY --from=build /srv/api/dist ./dist
COPY agent-host-platform/dashboard /srv/dashboard
ENV DASHBOARD_DIR=/srv/dashboard PORT=3000 NODE_ENV=production
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

Pass the secrets as container env (`-e` / a secrets manager), never baked
into the image.

## 4. Domain setup + HTTPS (reverse proxy sketch)

Terminate TLS in front of the API. The API itself speaks plain HTTP on
`PORT`; bearer tokens must never travel over plain HTTP in production.

**Caddy** (automatic HTTPS):

```
control-plane.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

**nginx** sketch:

```nginx
server {
    listen 443 ssl;
    server_name control-plane.example.com;
    ssl_certificate     /etc/letsencrypt/live/control-plane.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/control-plane.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        # SSE stream needs unbuffered, long-lived connections:
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

The dashboard (`/`) and the SSE stream (`/v1/events/stream`) ride the same
origin. Note: the SSE `?api_key=` fallback exists because `EventSource`
can't set headers — every other endpoint requires the `Authorization`
header.

## 5. Host worker installation

On the persistent host, as root (full reference:
[`host-install.md`](host-install.md)):

```bash
curl -fsSL https://<your-mirror>/Universal-AGT/scripts/install-host.sh -o /tmp/uagt-install.sh
sudo UAHT_CONTROL_PLANE_URL="https://control-plane.example.com" \
     UAHT_HOST_NAME="persistent-host-01" \
     bash /tmp/uagt-install.sh
```

Verify: `systemctl is-active agent-host-worker` → `active`, and the journal
shows heartbeats reaching the plane. From any agent key:

```bash
curl -s https://control-plane.example.com/v1/hosts \
  -H "Authorization: Bearer $UAHT_API_KEY" | python3 -m json.tool
# persistent-host-01: status "online", fresh last_seen, cpu/ram/disk stats
```

## 6. Agent registration — Muse + Instinct

Two agents, two permission sets (no agent-type special-casing exists
anywhere — authorization flows from `permissions` only):

```bash
CP="https://control-plane.example.com"
H="X-Provisioning-Token: <your-UAHT_PROVISIONING_TOKEN>"

# muse: full operator — deploy, restart/stop services, approve manual deploys,
# suspend/resume/revoke agents, assign project owners
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' -H "$H" \
  -d '{"name": "muse", "type": "ci", "capabilities": ["docker", "compose"],
       "permissions": {"deploy": true, "read_status": true,
                       "restart": true, "stop": true, "approve_deployments": true,
                       "admin": true}}'
# → 201 {"agent": {...}, "api_key": "<show once>"}

# instinct: deploy-only builder — cannot restart/stop/approve or touch secrets
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' -H "$H" \
  -d '{"name": "instinct", "type": "ci", "capabilities": ["docker"],
       "permissions": {"deploy": true, "read_status": true}}'
```

Store each key in that agent's secret storage. Permissions are fixed at
registration — to change them, register a new agent.

## 7. First deployment walkthrough

```bash
export UAHT_BASE_URL="https://control-plane.example.com"
export UAHT_API_KEY="<muse key>"

# project
agent-host projects create --name demo-app --runtime docker

# artifact (demo-app is zero-dependency Python; manifest at agent.deploy.json)
tar -czf /tmp/demo-app.tar.gz -C agent-host-platform/examples/demo-app .
SHA=$(sha256sum /tmp/demo-app.tar.gz | cut -d' ' -f1)

# deploy (upload + deploy in one step; automatic mode)
agent-host deploy --project demo-app --version 1.0.0 \
  --artifact /tmp/demo-app.tar.gz --host persistent-host-01

# watch
agent-host status --deployment <id>     # … → running, health_status healthy
agent-host logs --deployment <id>       # build + run logs
```

Or do the same flow with raw `curl` per [`agent-guide.md`](agent-guide.md).
Try a manual-mode deploy next (`--mode manual`): the task parks in
`awaiting_approval` — approve it in the dashboard's Approvals panel (it
shows a full dossier: action, project/version, host, artifact, resources,
domains) or `agent-host approve --task <id>`.

## 8. Cloudflare tunnel setup (optional public ingress)

Only if you want public hostnames for apps on a host with no public IP.
Full story: [`cloudflare.md`](cloudflare.md).

1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a
   tunnel**; copy the **tunnel token**; note `<tunnel-id>.cfargotunnel.com`.
2. In the tunnel's **Public hostnames** tab, add each app hostname you will
   serve (required for dashboard-created token tunnels).
3. Control plane env: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`,
   `TUNNEL_INGRESS_HOSTNAME=<tunnel-id>.cfargotunnel.com`.
4. Host `worker.env`: `WORKER_INGRESS_ENABLED=true`,
   `WORKER_INGRESS_PROVIDER=cloudflare-tunnel`,
   `WORKER_TUNNEL_TOKEN=<redacted>
   (installer input: `UAHT_TUNNEL_TOKEN`).
5. `agent-host domains add --deployment <id> --hostname api.example.com --ingress tunnel`
   → CNAME created, `ingress-sync` queued, worker writes the route table.

## 9. Backup & restore

The control plane is the source of truth; the host is disposable. Back
up accordingly — **the persistent host's local disk must never be the
only copy of critical control-plane state** (its `state.json` files are
worker-local caches; the durable records live in Postgres).

### What must be backed up

| Item | Where it lives | How to back it up |
|---|---|---|
| **Database** — agents, hosts, projects, artifacts (metadata), deployments, tasks, secrets (encrypted), events journal, port allocations, domains, `schema_migrations` | Postgres (Supabase) | `pg_dump` (Supabase dashboard → Database → Backups, or scheduled `pg_dump -Fc`). Include the schema, not just data: `schema_migrations` tracks which migrations applied. |
| **`DATA_ENCRYPTION_KEY`** | control-plane `.env` (`/etc/uagt/control-plane.env`) | Offline secret storage. **Losing it invalidates every stored project secret** (AES-256-GCM cannot decrypt them) — back it up once, on day one. |
| **Artifact bytes** | `ARTIFACT_DIR` (`./data/artifacts`) | Included in the control-plane host's filesystem backup. If lost: re-upload artifacts (each upload creates a new artifact row — redeploy against the new `artifact_id`); the DB's artifact *metadata* rows survive but point at missing bytes until then. |
| **Log chunks** | `LOG_DIR` (`./data/logs`) | Nice-to-have, not critical: task `result`/`error` summaries live in the DB. |
| **Provisioning token** | `UAHT_PROVISIONING_TOKEN` in the control-plane env | Same secret storage as the encryption key. |
| **Host identity** | DB `hosts` row + `WORKER_HOST_TOKEN` in the host's `/opt/agent-host/config/worker.env` (0600) | The token is shown once and stored only as a SHA-256 hash server-side — it **cannot be recovered from the DB**. After a host rebuild, re-register the host (or `POST /v1/hosts/:id/rotate-token` if you kept the old token) and write the new token to the fresh `worker.env`. |
| **Domain/DNS state** | DB `domains` table + Cloudflare (DNS records, tunnel routes) | DB backup covers intent; DNS/tunnel routes re-provision from the `domains` rows on `POST /v1/domains` retry or removal+re-add. Tunnel tokens live in the Cloudflare dashboard, not here. |

### Restore procedure (DB dump + artifact store)

Validated against the code paths (each step maps to a real mechanism;
the full drill needs a live Postgres — run it there before you need it):

1. Restore the database (`pg_restore` / Supabase restore). Verify
   `select * from schema_migrations order by name;` shows `001`…`012`.
2. **Re-apply migration `005`** (`REVOKE TRUNCATE ON public.events FROM
   PUBLIC`) — it is database-global and `pg_dump` schema-only dumps do
   not capture it.
3. Restore `ARTIFACT_DIR` from the filesystem backup. If the bytes are
   gone, re-upload each artifact (`agent-host deploy --artifact ...`
   creates a new artifact row) and redeploy — the DB's metadata rows
   alone cannot rebuild missing bytes.
4. Put the **original** `DATA_ENCRYPTION_KEY` and
   `UAHT_PROVISIONING_TOKEN` back in the control-plane env; start the
   API — startup runs `runMigrations`, which is a no-op when
   `schema_migrations` already lists `001`…`012`.
5. Reinstall/repair the host worker (`scripts/install-host.sh` with a
   fresh host token); the worker's local `state.json` rebuilds from
   Docker reality on first boot (`reconcile()`), and the control plane
   already knows every deployment's desired state.
6. For tunnel-mode domains, verify routes in the Cloudflare dashboard;
   re-run `agent-host domains add` for any hostname that fails its
   HTTPS probe.

### What is deliberately NOT backed up

- Host worker local state (`<work_dir>/deployments/*/state.json`,
  `DeploymentStore` monotonic `seq` counters) — rebuilt from Docker +
  the control plane on boot; `seq` is a per-store ordering aid, not a
  global truth.
- The `uag_events` LISTEN channel state — ephemeral by design; the
  journal itself is in the DB.
- Bearer tokens (agent keys, host tokens) — one-way SHA-256 hashes only.
  Rotate, don't restore.

## 10. Troubleshooting

See [`troubleshooting.md`](troubleshooting.md) for the symptom → cause →
fix table. The two fastest checks:

```bash
# API alive + migrations applied?
curl -s https://control-plane.example.com/v1/health
# Host checking in?
agent-host hosts   # status online, last_seen fresh
```

## 11. Production checklist

- [ ] Postgres (Supabase) with migrations 001–014 applied; 001 (pgcrypto)
      applied as superuser on a fresh database.
- [ ] `NODE_ENV=production`, `UAHT_PROVISIONING_TOKEN` set (API refuses to
      boot without it), `DATA_ENCRYPTION_KEY` backed up (64 hex chars).
- [ ] HTTPS in front of the API (Caddy/nginx per §4); SSE unbuffered.
- [ ] Agent keys scoped minimally (Muse: operator set incl.
      `approve_deployments`; Instinct: deploy-only). `manage_secrets` on as
      few keys as possible.
- [ ] Host token only in `/opt/agent-host/config/worker.env` (0600).
- [ ] `ARTIFACT_MAX_BYTES` sized for your artifacts.
- [ ] Dashboard approvers know: Approve/Reject need `approve_deployments`.
- [ ] Sweeper thresholds (`HEARTBEAT_*_S`, `TASK_*_S`) fit your network.
- [ ] First deploy + manual-mode approval + rollback exercised end to end.
- [ ] Backups: scheduled `pg_dump` of the database (incl. `schema_migrations`
      table), `DATA_ENCRYPTION_KEY` + `UAHT_PROVISIONING_TOKEN` in offline
      secret storage, `ARTIFACT_DIR` covered by the control-plane host's
      filesystem backup. Migration `005` re-applied after any DB restore.
