# Control Plane API

REST control plane for the Universal Agent-to-Persistent-Host Deployment
System. Implements `agent-sdk/protocol/PROTOCOL.md` exactly: agents,
task queue, host worker endpoints, projects, artifacts, deployments,
services, secrets, and the append-only event journal with an SSE stream.

Stack: TypeScript + Express + `pg`. No ORM. Agent-neutral — Muse,
Instinct, and custom agents are all just rows in `agents`.

## Setup

```bash
cd agent-host-platform/control-plane/api
cp .env.example .env        # then fill in DATABASE_URL + DATA_ENCRYPTION_KEY
npm install
npm run build
```

Generate the encryption key (32 bytes, hex):

```bash
openssl rand -hex 32   # paste into DATA_ENCRYPTION_KEY
```

## Running

Against **local Postgres**:

```bash
createdb universal_agt
# .env: DATABASE_URL=postgres://postgres:postgres@localhost:5432/universal_agt
npm run migrate   # applies database/migrations/*.sql in order
npm start         # listens on $PORT (default 3000); also auto-migrates on boot
```

Against **Supabase**: paste the project's connection string into
`DATABASE_URL` (the direct connection or the pooler URI both work —
the API uses plain parameterized SQL, no Supabase client needed), then
`npm run migrate && npm start`. The schema uses `pgcrypto`'s
`gen_random_uuid()`, which Supabase enables.

Migrations are tracked in `schema_migrations`; each file runs once,
inside its own transaction. The canonical schema is
`database/schema/schema.sql` (== `migrations/001_initial.sql`);
`002_artifact_status.sql` and `003_events_notify.sql` are additive.

## Bootstrap

1. Register the first (admin) agent — open endpoint, shown once:

```bash
curl -s -X POST localhost:3000/v1/agents/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"ops","type":"cli","permissions":{
    "deploy":true,"read_status":true,"read_logs":true,"restart":true,
    "stop":true,"remove":true,"manage_domains":true,
    "approve_deployments":true,"manage_secrets":true}}'
# -> { agent: {...}, api_key: "uag_..." }   # save the key; it is never shown again
export AGENT_KEY=uag_...
```

2. Register a host (two options — pick one):

```bash
# Option A: provisioning token (hands-off bootstrap, no agent key needed)
# .env: PROVISIONING_TOKEN=<openssl rand -hex 32>
curl -s -X POST localhost:3000/v1/hosts/register \
  -H "Authorization: Bearer $PROVISIONING_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"persistent-host-01","capabilities":["docker"]}'

# Option B: agent token with the deploy permission
curl -s -X POST localhost:3000/v1/hosts/register \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"persistent-host-01","capabilities":["docker"]}'
# -> { host: {...}, host_token: "uagh_..." }  # shown once; stored as SHA-256
```

Design choice: `POST /v1/hosts/register` accepts **either** a matching
`PROVISIONING_TOKEN` bearer (for unattended first-boot provisioning) **or**
an agent token carrying `deploy`. When `PROVISIONING_TOKEN` is unset, only
the agent path works.

## Auth

`Authorization: Bearer <token>` everywhere except `GET /v1/health` and
`POST /v1/agents/register`. Agent keys hit agent endpoints, host tokens
hit worker endpoints — a host token on an agent endpoint (and vice versa)
is 403. All failures use the PROTOCOL error shape:
`{ "error": { "code": "...", "message": "..." } }`.

## Endpoint map (all under /v1)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | /health | none | `{ok, version, time}` |
| POST | /agents/register | none | 201 `{agent, api_key}` (once); 409 dup name |
| GET | /agents/me | agent | own row, no secret fields |
| POST | /tasks | agent | `deploy` perm required for type=deploy, else `read_status`; idempotency key supported; `mode=manual` → `awaiting_approval` |
| GET | /tasks?status=&type=&host_id=&limit=&cursor= | agent+read_status | keyset pagination, `next_cursor` |
| GET | /tasks/:id | agent+read_status | |
| POST | /tasks/:id/cancel | agent+read_status | state-machine guarded; 409 from terminal states |
| POST | /tasks/:id/approve | agent+approve_deployments | awaiting_approval → queued |
| POST | /tasks/:id/reject | agent+approve_deployments | awaiting_approval → cancelled |
| POST | /hosts/register | provisioning token OR agent+deploy | 201 `{host, host_token}` (once) |
| GET | /hosts, /hosts/:id | agent+read_status | |
| POST | /hosts/:id/heartbeat | host (own id) | updates stats, sets online; `{host, pending_tasks}`; emits `host.online` on offline→online |
| POST | /worker/tasks/claim?wait=N | host | atomic `FOR UPDATE SKIP LOCKED` claim; 204 if none; long-polls up to 30s |
| POST | /worker/tasks/:id/progress | host (claiming host) | `{status, log_chunk?, result?, error?}`; state-machine validated; logs → `LOG_DIR/<id>.log` |
| POST/GET | /projects, /projects/:id | agent | POST/PUT need `deploy`; PUT validates `configuration.name` vs project name (PROTOCOL §4) |
| PUT | /projects/:id | agent+deploy | manifest validation |
| POST | /artifacts/init | agent+deploy | 201 `{artifact, upload_url}` |
| PUT | /artifacts/:id/content | agent+deploy | octet-stream; verifies size + sha256 → 422 + `failed` on mismatch |
| GET | /artifacts/:id, /artifacts?project_id= | agent+read_status | |
| GET | /artifacts/:id/download | agent+read_status OR host | streams bytes |
| POST | /deployments | agent+deploy | creates deployment + deploy task atomically; idempotency key dedupes both |
| GET | /deployments?project_id=&host_id=&status= | agent+read_status | |
| GET | /deployments/:id | agent+read_status | includes latest task |
| POST | /deployments/:id/rollback | agent+deploy | new deployment of previous healthy version; `deployment.rolled_back` emitted on worker completion |
| GET | /services?host_id= | agent+read_status | friendly view of lifecycle deployments |
| POST | /services/:id/restart\|stop\|start | agent | restart/stop/start perms; creates control task on the deployment's host |
| POST/GET/DELETE | /projects/:id/secrets | agent+manage_secrets | AES-256-GCM at rest; GET returns names only |
| GET | /events?type=&since=&limit=&cursor= | agent+read_status | ascending id keyset pagination |
| GET | /events/stream | agent+read_status | SSE: replays `?limit=N`, then live-pushes via pg LISTEN/NOTIFY (+ 5s poll backstop) |

The dashboard (built by the dashboard team into `DASHBOARD_DIR`,
default `agent-host-platform/dashboard`) is served as static files at `/`.

## Task state machine

`queued → claimed → running → completed|failed`
(`running → awaiting_approval → queued|cancelled` in manual mode;
`cancelled` from any non-terminal state; `retrying → queued`.)
Pure functions in `src/lib/stateMachine.ts`, unit-tested with zero DB.

## Notes / deliberate simplifications

- `host.offline` is not emitted by the API — no sweeper marks stale hosts
  offline yet; heartbeats set `online`, operators can query `last_seen`.
  (A periodic offline-sweeper is a natural follow-up.)
- Cancelling a deploy task does not rewrite the deployment row's status;
  the worker's terminal report is the source of deployment state.
- Secrets are injected into deploy task payloads by the worker flow —
  `getDecryptedProjectSecrets()` is available server-side for that; secret
  plaintext never appears in API responses, logs, or events.
- Rate limits: 120 req/min per agent key, 600 req/min per host token
  (in-memory sliding window; tune via `RATE_LIMIT_*`).
- No IP addresses are stored or returned anywhere; hosts are referenced
  by id/name only.
