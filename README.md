# Universal AGT — Universal Agent-to-Persistent-Host Deployment System

Agents are temporary; hosts are persistent. Universal AGT lets autonomous
agents (LLM-driven, scripted, or humans via CLI) **build apps, package them,
and deploy them to a persistent Linux host** — without the agent staying
online, without SSH, and without inbound connections to the host.

An agent registers, uploads an artifact, and creates a deployment. The
**control plane** (the source of truth) queues the work; the **host worker**
(an outbound-only daemon on the persistent host) claims it, builds and runs
the container, and reports back. The agent reads the result whenever it
comes back — minutes or days later.

Three layers, one wire protocol:

```
agents / SDKs / CLI ──HTTPS──▶ control plane ──HTTPS──▶ host worker
 (may disappear)        (durable state)          (outbound-only daemon)
```

## Repository layout

```
Universal-AGT/
├── README.md                        ← this file
└── agent-host-platform/
    ├── agent-sdk/
    │   ├── protocol/PROTOCOL.md     ← canonical wire contract (REST + SSE)
    │   ├── python/                  ← Python SDK (client, errors, SSE)
    │   └── javascript/              ← JavaScript SDK
    ├── cli/                         ← `agent-host` CLI (deploy, status, logs, rollback, …)
    ├── control-plane/               ← REST API, task queue, artifacts,
    │                                  deployments, events, auth (Node/TS)
    ├── database/                    ← schema + migrations (Postgres)
    ├── host-worker/                 ← outbound-only daemon: claim tasks,
    │                                  docker builds, health checks, updater
    ├── dashboard/                   ← static ops dashboard (this build)
    │   ├── index.html
    │   ├── css/style.css
    │   └── js/app.js
    ├── docs/                        ← guides (this build)
    │   ├── architecture.md          ← three layers, deploy data-flow, resumability
    │   ├── api.md                   ← endpoint reference summary
    │   ├── host-install.md          ← installing the worker on a host
    │   ├── agent-guide.md           ← register → upload → deploy → operate
    │   └── security.md              ← auth, allowlist, secrets, rate limits
    ├── examples/                    ← tiny real deployable apps (this build)
    │   ├── url-shortener/           ← Node http, zero deps
    │   ├── static-site/             ← nginx static page
    │   └── validate.py              ← manifest validator (PROTOCOL §4)
    └── scripts/
```

## Quickstart

### 1. Start the control plane

```bash
cd agent-host-platform/control-plane/api
npm install
npm run migrate        # apply database/migrations against Postgres
npm start              # serves the REST API at /v1 and the dashboard at /
```

`GET /v1/health` should return `{ok: true, version, time}`.

### 2. Install the worker on your persistent host

On the host (needs Docker + outbound HTTPS to the control plane):

```bash
sudo UAGT_CONTROL_PLANE="https://control-plane.example.com" \
     UAGT_HOST_TOKEN="<token from POST /v1/hosts/register>" \
     bash agent-host-platform/host-worker/install.sh
```

Full steps, verification, and uninstall: [`docs/host-install.md`](agent-host-platform/docs/host-install.md).

### 3. Deploy an example

```bash
# one step: upload the artifact and deploy (see docs/agent-guide.md §7)
export UAHT_BASE_URL="https://control-plane.example.com"
export UAHT_API_KEY="<your agent api key>"
tar -czf /tmp/url-shortener.tar.gz -C agent-host-platform/examples/url-shortener .
agent-host deploy --project url-shortener --version 1.0.0 \
  --artifact /tmp/url-shortener.tar.gz --mode automatic

# or watch it happen live in the dashboard served at /
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
  with no lock service.
- **Manual approval mode.** Tasks can park in `awaiting_approval` until a
  holder of `approve_deployments` approves or rejects them.
- **Append-only events.** Everything security-relevant is journaled and
  streamed live over SSE at `/v1/events/stream`.
- **Defense in depth.** SHA-256-hashed bearer tokens, scoped permissions,
  worker command allowlist (never a raw shell string from the network),
  double-verified artifact checksums, encrypted secrets that are never
  returned by reads, and per-key rate limits. See
  [`docs/security.md`](agent-host-platform/docs/security.md).

## Docs

| Doc | What it covers |
|---|---|
| [`agent-sdk/protocol/PROTOCOL.md`](agent-host-platform/agent-sdk/protocol/PROTOCOL.md) | **Canonical** wire protocol — REST + JSON + SSE, auth, manifest rules |
| [`docs/architecture.md`](agent-host-platform/docs/architecture.md) | Three layers, ASCII diagram, deploy data-flow, resumability/idempotency |
| [`docs/api.md`](agent-host-platform/docs/api.md) | Endpoint reference summary |
| [`docs/host-install.md`](agent-host-platform/docs/host-install.md) | Worker install, systemd, verification, uninstall |
| [`docs/agent-guide.md`](agent-host-platform/docs/agent-guide.md) | Agent/CLI deploy walkthrough, approval modes |
| [`docs/security.md`](agent-host-platform/docs/security.md) | Auth model, permissions, allowlist, secrets, rate limits |

## Conventions

- No IP addresses anywhere in requests, responses, or docs — hosts are
  referenced by `host_id` / `host_name` only.
- No secrets in the repo. Tokens are shown once at registration and live in
  secret storage / root-only files.
- Every mutating call should carry an `idempotency_key` — retries are safe
  by construction.
