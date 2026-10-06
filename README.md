# Universal AGT — Universal Agent-to-Persistent-Host Deployment System

Agents are temporary; hosts are persistent. Universal AGT lets autonomous
agents (LLM-driven, scripted, or humans via CLI) **build apps, package them,
and deploy them to a persistent Linux host** — without the agent staying
online, without SSH, and without inbound connections to the host.

An agent registers, uploads an artifact, and creates a deployment. The
**control plane** (the source of truth) queues the work; the **host worker**
(an outbound-only daemon on the persistent host) claims it, builds and runs
the container, and reports back. The agent reads the result whenever it
comes back — minutes or days later. An optional **public edge** (Cloudflare
DNS + a worker-managed outbound tunnel) serves the deployed apps to the
public internet without ever opening an inbound port on the host.

Four layers, one wire protocol:

```
LAYER 1: AGENTS            agents / SDKs (Python, JS) / `agent-host` CLI / humans
  (may disappear)                 │  HTTPS + Bearer agent API key
                                  ▼
LAYER 2: CONTROL PLANE     REST API + durable Postgres state: agents, hosts,
  (durable state)                  projects, artifact store (sha256), deployments,
                                   task queue (atomic claims), encrypted secrets,
                                   append-only event journal, SSE stream.
                                   Serves the ops dashboard at `/`.
                                  │  HTTPS + Bearer host token (worker dials out)
                                  ▼
LAYER 3: HOST WORKER        systemd daemon on the persistent host: heartbeats,
  (outbound-only daemon)           claims tasks, builds/runs containers (Docker),
                                   health checks, crash-loop detection,
                                   post-reboot reconciliation, self-updates.
                                   Optionally supervises `cloudflared` (Phase 7).
                                  │  public traffic (only if ingress enabled)
                                  ▼
LAYER 4: PUBLIC EDGE       Cloudflare: proxied CNAMEs (DNS API) + edge/tunnel.
  (optional, Phase 7)              The tunnel dials OUT to Cloudflare's edge —
                                   the host still listens on nothing.
```

## Repository layout

```
Universal-AGT/
├── README.md                        ← this file
├── .github/workflows/ci.yml         ← 12 jobs: control-plane, migration-validation, js-sdk, host-worker, installer, python-sdk, cli, dashboard, security, secrets-sweep, dist-drift-guard, e2e (API against real PostgreSQL 16)
└── agent-host-platform/
    ├── agent-sdk/
    │   ├── protocol/PROTOCOL.md     ← canonical wire contract (REST + SSE) — authoritative
    │   ├── python/                  ← Python SDK (client, SSE, deploy helper)
    │   └── javascript/              ← JavaScript SDK (client, SSE async generator)
    ├── cli/                         ← `agent-host` CLI (deploy, status, logs, rollback, approve, …)
    ├── control-plane/
    │   ├── api/                     ← Express REST API (Node/TS): routes, sweepers, retry policy
    │   ├── agents/ hosts/ task-queue/ deployments/ artifacts/ events/ authentication/
    │   └── (modules live under api/src; the top-level dirs mirror the design areas)
    ├── database/migrations/         ← 001_initial … 012_rollback_failed_status (apply in order)
    ├── host-worker/                 ← outbound-only daemon: claim tasks, docker builds,
    │                                  health checks, reconcile, updater, ingress/
    ├── dashboard/                   ← static ops dashboard served by the API at `/`
    │                                  (approvals panel, actions, SSE live view)
    ├── docs/                        ← guides (see table below)
    ├── examples/                    ← tiny real deployable apps
    │   ├── demo-app/                ← zero-dep Python app (used by the E2E suite)
    │   ├── url-shortener/           ← Node http, zero deps
    │   ├── static-site/             ← nginx static page
    │   └── validate.py              ← manifest validator (PROTOCOL §4)
    └── scripts/
        └── install-host.sh          ← host installer (see docs/host-install.md)
```

## Quickstart

### 1. Start the control plane

```bash
cd agent-host-platform/control-plane/api
npm ci
npm run build
# apply migrations 001…012 against Postgres first, then:
DATABASE_URL="postgresql://user:pass@localhost:5432/uagt" \
DATA_ENCRYPTION_KEY="$(openssl rand -hex 32)" \
UAHT_PROVISIONING_TOKEN="<pick-a-strong-value>" \
npm start                       # REST API at /v1, dashboard at /
```

`GET /v1/health` should return `{ok: true, version, time}`. Full production
guide: [`docs/deployment.md`](agent-host-platform/docs/deployment.md).

### 2. Install the worker on your persistent host

On the host (needs Docker + outbound HTTPS to the control plane), as root:

```bash
curl -fsSL https://<your-mirror>/Universal-AGT/scripts/install-host.sh -o /tmp/uagt-install.sh
sudo UAHT_CONTROL_PLANE_URL="https://control-plane.example.com" \
     UAHT_HOST_NAME="persistent-host-01" \
     bash /tmp/uagt-install.sh
```

The installer provisions the host at the control plane, writes
`/opt/agent-host/config/worker.env` (mode `0600`), installs the
`agent-host-worker` systemd unit, starts it, and verifies the first
heartbeat. Full steps, verification, ingress, and uninstall:
[`docs/host-install.md`](agent-host-platform/docs/host-install.md).

### 3. Deploy an example

```bash
# one step: upload the artifact and deploy (see docs/agent-guide.md)
export UAHT_BASE_URL="https://control-plane.example.com"
export UAHT_API_KEY="<your agent api key>"     # from POST /v1/agents/register (shown once)
tar -czf /tmp/demo-app.tar.gz -C agent-host-platform/examples/demo-app .
agent-host deploy --project demo-app --version 1.0.0 \
  --artifact /tmp/demo-app.tar.gz --mode automatic

# watch it happen live in the dashboard served at /
```

The walkthrough with raw `curl` (register → artifact upload → deploy →
status polling, automatic vs manual approval): [`docs/agent-guide.md`](agent-host-platform/docs/agent-guide.md).

### 4. Validate an example manifest

```bash
python3 agent-host-platform/examples/validate.py
```

## Core concepts

- **Tasks are the durable work queue.** `POST /v1/tasks` with an
  `idempotency_key`; the task row is the source of truth, so a vanished
  agent's work still completes and its result is readable later.
- **Deployments wrap tasks.** `POST /v1/deployments` creates a deployment
  row plus a `deploy` task; statuses flow
  `requested → building → starting → healthcheck → running`.
- **Atomic claims.** Exactly one host gets a queued task — multi-host safe
  with no lock service (`FOR UPDATE SKIP LOCKED`).
- **Claim leases + sweepers.** Every claim records a lease
  (`TASK_CLAIM_LEASE_S`, default 600s), refreshed on each progress report.
  The stuck-task sweeper requeues lease-expired work or fails it when the
  retry budget is exhausted; the stale-host sweeper marks silent hosts
  `degraded`/`offline` (`HEARTBEAT_*_S` thresholds).
- **Retry policy.** Failed tasks become `retrying` only when the type is
  safe to re-run and `attempts < max_attempts`; `remove`/`rollback` never
  auto-retry.
- **Port registry.** Fixed `host_port` reservations are unique per
  host+port (409 on collision); the worker also bind-tests every port at
  OS and Docker level before `docker run`.
- **Manual approval mode.** Tasks park in `awaiting_approval` at creation
  until a holder of `approve_deployments` approves or rejects them — in the
  API, the CLI, and the dashboard's Approvals panel.
- **Append-only events.** Everything security-relevant is journaled and
  streamed live over SSE at `/v1/events/stream`.
- **Secrets that stay secret.** AES-256-GCM at rest, never returned by
  reads, decrypted only for the host executing the deploy, injected as
  container env, never written to worker disk.
- **Public ingress without inbound ports (Phase 7).** DNS alone cannot
  reach an outbound-only host — so the worker can supervise an outbound
  `cloudflared` tunnel and sync `hostname → 127.0.0.1:port` routes for
  tunnel-mode domains. See [`docs/cloudflare.md`](agent-host-platform/docs/cloudflare.md).
- **Defense in depth.** SHA-256-hashed bearer tokens, scoped permissions,
  worker command allowlist (never a raw shell string from the network),
  double-verified artifact checksums, wire-capped uploads, per-key rate
  limits. See [`docs/security.md`](agent-host-platform/docs/security.md).

## Docs

| Doc | What it covers |
|---|---|
| [`agent-sdk/protocol/PROTOCOL.md`](agent-host-platform/agent-sdk/protocol/PROTOCOL.md) | **Canonical** wire protocol — REST + JSON + SSE, auth, manifest rules, changelog |
| [`docs/architecture.md`](agent-host-platform/docs/architecture.md) | Four layers, deploy data-flow, sweepers, retry policy, port registry, ingress |
| [`docs/api.md`](agent-host-platform/docs/api.md) | Endpoint reference summary |
| [`docs/deployment.md`](agent-host-platform/docs/deployment.md) | **Production deployment guide**: Supabase/migrations, env vars, domain + HTTPS, first deploy |
| [`docs/host-install.md`](agent-host-platform/docs/host-install.md) | Worker install, systemd, verification, ingress, uninstall |
| [`docs/cloudflare.md`](agent-host-platform/docs/cloudflare.md) | The three ingress modes: metadata-only, direct, cloudflare-tunnel |
| [`docs/agent-guide.md`](agent-host-platform/docs/agent-guide.md) | Agent deploy walkthrough (register → deploy → operate) |
| [`docs/agent-integration.md`](agent-host-platform/docs/agent-integration.md) | Machine-readable integration card: every endpoint, task types, error codes |
| [`docs/security.md`](agent-host-platform/docs/security.md) | Auth model, permissions, worker containment, artifact integrity, rate limits |
| [`docs/troubleshooting.md`](agent-host-platform/docs/troubleshooting.md) | Symptom → cause → fix for the real failure modes |
| [`docs/e2e-live-checklist.md`](agent-host-platform/docs/e2e-live-checklist.md) | 14 live-infrastructure checks (Supabase + real host + real Docker) |
| [`docs/live-acceptance.md`](agent-host-platform/docs/live-acceptance.md) | **Live acceptance**: production smoke-test procedure, required env vars, and the final acceptance checklist |
| [`docs/implementation-status.md`](agent-host-platform/docs/implementation-status.md) | Acceptance scorecard + per-phase resolution log |

## Conventions

- No IP addresses anywhere in requests, responses, or docs — hosts are
  referenced by `host_id` / `host_name` only.
- No secrets in the repo. Tokens are shown once at registration and live in
  secret storage / root-only files. Examples use `<placeholder>` values.
- Every mutating call should carry an `idempotency_key` — retries are safe
  by construction.
- Agent-neutral by design: Muse, Instinct, CI agents, and humans are all
  just agent rows with scoped permission sets — no agent-type special-casing
  anywhere in the control plane, SDKs, CLI, or worker.
