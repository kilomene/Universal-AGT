# Universal AGT — API Reference (summary)

Canonical contract: [`../agent-sdk/protocol/PROTOCOL.md`](../agent-sdk/protocol/PROTOCOL.md).
That document is authoritative; this page is a quick map. Base URL is
`https://<control-plane-host>/v1`, JSON everywhere, bearer tokens as
`Authorization: Bearer <token>`.

## Agents

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/agents/register` | `X-Provisioning-Token` header (or bootstrap: first agent only, when `UAHT_PROVISIONING_TOKEN` is unset) | Register an agent; returns `api_key` **once** |
| GET | `/v1/agents/me` | agent key | Caller's own agent row (no secrets) |
| POST | `/v1/agents/me/rotate` | agent key | Rotate the caller's API key; returns the **new** key once |
| POST | `/v1/agents/:id/suspend` | **operator** (`admin` permission or provisioning token) | `active → suspended`; the key is rejected immediately; self-suspend → 403 |
| POST | `/v1/agents/:id/resume` | **operator** | `suspended → active` (revoked agents cannot be resumed) |
| POST | `/v1/agents/:id/revoke` | **operator** | `→ revoked`; key hash replaced with an unmatchable random value — permanent; self-revoke → 403 |

## Tasks — durable work queue

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/tasks` | agent key (per-type permission — see below) | Create a task; accepts `idempotency_key` |
| GET | `/v1/tasks/:id` | agent key (`read_status`) | One task |
| GET | `/v1/tasks?status=&type=&host_id=&limit=&cursor=` | agent key (`read_status`) | List / filter tasks |
| POST | `/v1/tasks/:id/cancel` | agent key (`deploy` **or** the task's creating agent) | Move a non-terminal task to `cancelled` |
| POST | `/v1/tasks/:id/approve` | agent key (`approve_deployments`) | `awaiting_approval → queued` |
| POST | `/v1/tasks/:id/reject` | agent key (`approve_deployments`) | `awaiting_approval → cancelled` |

Task types: `deploy`, `restart`, `stop`, `start`, `remove`, `rollback`,
`logs`, `status`, `healthcheck`, `build`, `docker-build`, `docker-run`,
`docker-compose`, `environment-update`, `artifact-download`,
`artifact-upload`, `system-info`, `ingress-sync`. Payload shapes per type
are in PROTOCOL §5.

**Per-type permission for `POST /v1/tasks`** (enforced in `routes/tasks.ts`):
`deploy` for `deploy`, `remove`, `rollback`, `build`, `docker-build`,
`docker-run`, `docker-compose`, `environment-update`, `artifact-upload`,
`ingress-sync`; `restart` for `restart`, `start`; `stop` for `stop`;
`read_status` for `logs`, `status`, `healthcheck`, `system-info`,
`artifact-download`.

**Approve side effect:** `POST /v1/tasks/:id/approve` also flips the task's
linked deployment to `approved` and emits `deployment.approved`.

Status machine: `queued → claimed → running → completed | failed`, with
`awaiting_approval` parked *before* `queued` in manual mode (the task is
created there; approve → `queued`), `retrying` as the requeue hop for
retryable failures, and `cancelled` reachable from any non-terminal state.

## Hosts

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/hosts` | agent key (`read_status`) | All hosts |
| GET | `/v1/hosts/:id` | agent key (`read_status`) | Host + last heartbeat stats + running apps |
| POST | `/v1/hosts/register` | `X-Provisioning-Token` header **or** `Authorization: Bearer <token>` (`UAHT_PROVISIONING_TOKEN`), **or** agent key with `deploy` | Register a host; returns `host_token` **once** |
| POST | `/v1/hosts/:id/rotate-token` | host token | Rotate the host's token; returns the **new** token once |

## Worker-facing (host token only)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/hosts/:id/heartbeat` | host token | Stats + running apps; returns `pending_tasks` |
| POST | `/v1/worker/tasks/claim?wait=25` | host token | Atomically claim one queued task (204 if none) |
| POST | `/v1/worker/tasks/:id/progress` | host token | Status + log chunks + result/error |
| GET | `/v1/worker/domains` | host token | Tunnel-mode domains for the ingress route table |
| GET | `/v1/worker/projects/:project_id/secrets` | host token | Decrypted project secrets (only with live work for that project) |

A host token can never call agent endpoints and vice versa.

## Projects & artifacts

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/projects` | agent key (`deploy`) | Create a project (the caller becomes `owner_agent_id`) |
| GET | `/v1/projects` | agent key (`read_status`) | List projects (only ones the agent may access) |
| GET | `/v1/projects/:id` | agent key (`read_status`) | One project |
| PUT | `/v1/projects/:id` | agent key (`deploy`) | Update `configuration` / `repository` / `runtime` (validated manifest) |
| GET | `/v1/projects/:id/members` | agent key (`read_status`) | Project ACL membership |
| POST | `/v1/projects/:id/members` | agent key (`deploy`) | Grant an agent access (`{agent_id}`) |
| DELETE | `/v1/projects/:id/members/:agent_id` | agent key (`deploy`) | Revoke an agent's access |
| POST | `/v1/projects/:id/owner` | **operator** (`admin` permission or provisioning token) | Assign/reassign `owner_agent_id` (`{agent_id}`) |
| POST | `/v1/artifacts/init` | agent key (`deploy`) | Start an upload; returns `upload_url`. Optional `manifest` (the artifact's `agent.deploy.json`): validated with the worker's grammar, stored on the row, and used by the scheduler as the resource source of truth (see `docs/resource-contract.md`) |
| PUT | `/v1/artifacts/:id/content` | agent key (`deploy`) | Upload bytes (`application/octet-stream`); server verifies size + SHA-256 |
| GET | `/v1/artifacts/:id` | agent key (`read_status`) | Artifact metadata |
| GET | `/v1/artifacts?project_id=` | agent key (`read_status`) | List artifacts for a project |
| GET | `/v1/artifacts/:id/download` | agent or host token | Stream artifact bytes |

## Deployments & services

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/deployments` | agent key (`deploy`) | Deploy; creates deployment row + task; accepts `idempotency_key`, optional `host_port`; `host_id: null` = control plane selects the host (online, not draining, capabilities, CPU/RAM headroom, host ACL, atomic via row locks); 503 `no_capacity` when no host is eligible |
| GET | `/v1/deployments?project_id=&host_id=&status=&limit=` | agent key (`read_status`) | List deployments |
| GET | `/v1/deployments/:id` | agent key (`read_status`) | Deployment (+ its task) |
| POST | `/v1/deployments/:id/rollback` | agent key (`deploy`) | Roll back to previous healthy version |
| POST | `/v1/deployments/:id/settle-rollback-ports` | **host token** (must own the deployment) | Worker post-rollback port settlement; body `{target_deployment_id}`; emits `deployment.ports_settled` |
| GET | `/v1/services?host_id=&limit=` | agent key (`read_status`) | Running deployments (friendly view — the dashboard's Applications panel) |
| POST | `/v1/services/:id/restart` | agent key (`restart`) | Restart a service |
| POST | `/v1/services/:id/stop` | agent key (`stop`) | Stop a service |
| POST | `/v1/services/:id/start` | agent key (`restart`) | Start a service |

Deployment statuses: `requested`, `approved`, `building`, `starting`,
`healthcheck`, `running`, `failed`, `rolled_back`, `rollback_failed`
(a failed rollback — terminal and non-routable, persisted honestly rather
than recorded as `rolled_back`), `stopped`, `stopping`.

## Domains

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/domains` | agent key (`manage_domains`) | Full provision flow: `{"deployment_id","hostname","ingress":"tunnel"\|"direct"}` → validate → DNS → tunnel route → verify → `active` (or `failed`) |
| GET | `/v1/domains?deployment_id=` | agent key (`manage_domains`) | List domain rows (lifecycle state, per-step flags, error) |
| GET | `/v1/domains/:hostname` | agent key (`manage_domains`) | One domain's full lifecycle state |
| DELETE | `/v1/domains` | agent key (`manage_domains`) | Detach: delete tunnel route + DNS record, verify gone (`{"deployment_id","hostname"}`) |

Domain lifecycle: `requested → configuring → active → failed`, with
`degraded` as a live-but-unverified state (`active → degraded → active`
on recovery, `→ failed` when unrecoverable), and `removing → removed` on
detach. A domain attached to a deployment that reaches a terminal state
is marked `failed` (routes dropped, DNS removed, event emitted); the
domain reconciler (`DOMAIN_RECONCILE_INTERVAL_S`) periodically
re-verifies active domains against the authoritative remote tunnel
configuration and emits `domain.reconcile` (plus `domain.degraded` /
`domain.recovered` on transitions); see [`cloudflare.md`](cloudflare.md)
for the two ingress modes and the remote-vs-local control authority
statement — DNS alone cannot reach an outbound-only host.

## Secrets

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/projects/:id/secrets` | agent key (`manage_secrets`) | Store a secret (encrypted at rest, never returned) |
| GET | `/v1/projects/:id/secrets` | agent key (`manage_secrets`) | List secret **names** only |
| DELETE | `/v1/projects/:id/secrets/:name` | agent key (`manage_secrets`) | Delete a secret |

## Events (append-only journal)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/events?type=&since=&limit=&cursor=` | agent key (`read_status`) | Paginated event history |
| GET | `/v1/events/stream` | agent key (`read_status`) | SSE live stream, `data: {event}\n\n` per event |

Canonical event types (49 — every type the server emits, verified by
extracting all event literals from `control-plane/api/src/routes/` +
`lib/` + `middleware/` 2026-10-06; the "46" count predates the agent
lifecycle events):
`agent.connected`, `agent.key_rotated`, `agent.suspended`,
`agent.resumed`, `agent.revoked`, `auth.failed`,
`task.created/claimed/started/retrying/awaiting_approval/approved/rejected/completed/failed/cancelled`,
`task.requeued`,
`artifact.created`, `artifact.upload_failed`,
`deployment.requested/approved/started/completed/failed/rolled_back`,
`deployment.rollback_requested`, `deployment.rollback_failed`,
`deployment.ports_settled`,
`service.started/stopped/restarted/removed`, `service.crash_loop`,
`domain.requested/active/degraded/recovered/failed/removed`,
`domain.remove_failed`, `domain.reconcile`,
`host.registered/online/offline/degraded`, `host.token_rotated`,
`host.worker_outdated`,
`secret.changed`, `secret.deleted`.
Treat unknown types as opaque.

(2026-10-06 correction, synced in PROTOCOL §3.8 the same day: earlier
revisions of these docs said 41 — the W15 contract audit missed
`auth.failed` (emitted on bad provisioning token, `middleware/auth.ts`),
`host.worker_outdated` (heartbeat with an old worker, `routes/worker.ts`),
and the domain reconciler's `domain.degraded`/`domain.recovered`/
`domain.reconcile` (`routes/domains.ts`, `lib/domainReconciler.ts`).)

## Misc

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/health` | none | `{ok: true, version, time}` |

## Errors

All failures return `{ "error": { "code": "...", "message": "..." } }`.
Codes: `bad_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`
(idempotency key reused with a different payload), `unprocessable`
(manifest/validation failure), `payload_too_large` (413 — artifact over
`ARTIFACT_MAX_BYTES`), `no_capacity` (503 — `POST /v1/deployments` with
`host_id: null` and no eligible host), `rate_limited`.
