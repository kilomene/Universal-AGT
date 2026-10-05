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
  `005` (Supabase's SQL editor runs as superuser — fine).
- A Linux VM/container for the control plane (Node 20+, outbound HTTPS).
- One persistent Linux host (x86_64 or arm64) with Docker, for the worker.
- A domain name you control (only needed for public ingress / HTTPS).
- `openssl` for generating secrets.

## 1. Database: Supabase setup + migrations in order

1. Create a Supabase project; note the **connection string** (use the
   transaction pooler URI if your client is serverless — the API uses a
   `pg` pool, `PG_POOL_MAX=10` by default).
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
| 001 | `001_initial.sql` | Core tables: agents, hosts, projects, artifacts, deployments, tasks, secrets, events, port_allocations |
| 002 | `002_artifact_status.sql` | Artifact upload status lifecycle |
| 003 | `003_events_notify.sql` | `pg LISTEN/NOTIFY` plumbing for the SSE event bus |
| 004 | `004_phase3_reliability.sql` | Claim leases, retry bookkeeping, port registry, `draining` host state |
| 005 | `005_events_truncate_block.sql` | **Event trigger** aborting `TRUNCATE` on `events` (the append-only journal) — **needs superuser** (row triggers can't block `TRUNCATE`). Re-apply after any database restore. |

Verify:

```sql
-- in the Supabase SQL editor
select * from schema_migrations order by version;  -- 001…005 present
```

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

`npm start` runs the compiled output in `dist/`, so it **requires a prior
`npm run build`** — `dist/` is never committed to the repo. Use
`npm run prod` (build + start in one step) when you want a single command:

```bash
npm run prod     # npm run build && npm start
```

`npm start` runs migrations automatically; `npm run migrate` does it
explicitly. Health check: `GET /v1/health` → `{ok: true, version, time}`.

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

# muse: full operator — deploy, restart/stop services, approve manual deploys
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' -H "$H" \
  -d '{"name": "muse", "type": "ci", "capabilities": ["docker", "compose"],
       "permissions": {"deploy": true, "read_status": true, "read_logs": true,
                       "restart": true, "stop": true, "approve_deployments": true}}'
# → 201 {"agent": {...}, "api_key": "<show once>"}

# instinct: deploy-only builder — cannot restart/stop/approve or touch secrets
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' -H "$H" \
  -d '{"name": "instinct", "type": "ci", "capabilities": ["docker"],
       "permissions": {"deploy": true, "read_status": true, "read_logs": true}}'
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

## 9. Troubleshooting

See [`troubleshooting.md`](troubleshooting.md) for the symptom → cause →
fix table. The two fastest checks:

```bash
# API alive + migrations applied?
curl -s https://control-plane.example.com/v1/health
# Host checking in?
agent-host hosts   # status online, last_seen fresh
```

## 10. Production checklist

- [ ] Postgres (Supabase) with migrations 001–005 applied; 005 applied as
      superuser; re-applied after any restore.
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
