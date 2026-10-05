# Universal AGT — API Reference (summary)

Canonical contract: [`../agent-sdk/protocol/PROTOCOL.md`](../agent-sdk/protocol/PROTOCOL.md).
That document is authoritative; this page is a quick map. Base URL is
`https://<control-plane-host>/v1`, JSON everywhere, bearer tokens as
`Authorization: Bearer <token>`.

## Agents

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/agents/register` | — (one-time bootstrap) | Register an agent; returns `api_key` **once** |
| GET | `/v1/agents/me` | agent key | Caller's own agent row (no secrets) |

## Tasks — durable work queue

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/tasks` | agent key (`deploy` for deploy-type) | Create a task; accepts `idempotency_key` |
| GET | `/v1/tasks/:id` | agent key | One task |
| GET | `/v1/tasks?status=&type=&host_id=&limit=&cursor=` | agent key | List / filter tasks |
| POST | `/v1/tasks/:id/cancel` | agent key | Move a non-terminal task to `cancelled` |
| POST | `/v1/tasks/:id/approve` | agent key (`approve_deployments`) | `awaiting_approval → queued` |
| POST | `/v1/tasks/:id/reject` | agent key (`approve_deployments`) | `awaiting_approval → cancelled` |

Task types: `deploy`, `restart`, `stop`, `start`, `remove`, `rollback`,
`logs`, `status`, `healthcheck`, `build`, `docker-build`, `docker-run`,
`docker-compose`, `environment-update`, `artifact-download`,
`artifact-upload`, `system-info`. Payload shapes per type are in PROTOCOL §5.

Status machine: `queued → claimed → running → completed | failed`, with
`awaiting_approval` parked between `running` and `queued` in manual mode and
`cancelled` reachable from any non-terminal state.

## Hosts

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/hosts` | agent key (`read_status`) | All hosts |
| GET | `/v1/hosts/:id` | agent key (`read_status`) | Host + last heartbeat stats + running apps |
| POST | `/v1/hosts/register` | — (one-time bootstrap) | Register a host; returns `host_token` **once** |

## Worker-facing (host token only)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/hosts/:id/heartbeat` | host token | Stats + running apps; returns `pending_tasks` |
| POST | `/v1/worker/tasks/claim?wait=25` | host token | Atomically claim one queued task (204 if none) |
| POST | `/v1/worker/tasks/:id/progress` | host token | Status + log chunks + result/error |

A host token can never call agent endpoints and vice versa.

## Projects & artifacts

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/projects` | agent key | Create a project |
| GET | `/v1/projects` | agent key | List projects |
| GET | `/v1/projects/:id` | agent key | One project |
| PUT | `/v1/projects/:id` | agent key | Update configuration (validated manifest) |
| POST | `/v1/artifacts/init` | agent key | Start an upload; returns `upload_url` |
| PUT | `/v1/artifacts/:id/content` | agent key | Upload bytes (`application/octet-stream`); server verifies size + SHA-256 |
| GET | `/v1/artifacts/:id` | agent key | Artifact metadata |
| GET | `/v1/artifacts?project_id=` | agent key | List artifacts for a project |
| GET | `/v1/artifacts/:id/download` | agent or host token | Stream artifact bytes |

## Deployments & services

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/deployments` | agent key (`deploy`) | Deploy; creates deployment row + task; accepts `idempotency_key` |
| GET | `/v1/deployments?project_id=&host_id=&status=` | agent key | List deployments |
| GET | `/v1/deployments/:id` | agent key | Deployment (+ its task) |
| POST | `/v1/deployments/:id/rollback` | agent key (`deploy`) | Roll back to previous healthy version |
| GET | `/v1/services?host_id=` | agent key | Running deployments (friendly view — the dashboard's Applications panel) |
| POST | `/v1/services/:id/restart` | agent key (`restart`) | Restart a service |
| POST | `/v1/services/:id/stop` | agent key (`stop`) | Stop a service |
| POST | `/v1/services/:id/start` | agent key (`restart`) | Start a service |

Deployment statuses: `requested`, `approved`, `building`, `starting`,
`healthcheck`, `running`, `failed`, `rolled_back`, `stopped`, `stopping`.

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

Canonical event types: `agent.connected`, `agent.disconnected`,
`task.created/claimed/started/awaiting_approval/approved/rejected/completed/failed/cancelled`,
`artifact.created`, `artifact.upload_failed`,
`deployment.requested/approved/started/building/healthcheck/completed/failed/rolled_back`,
`service.started/stopped/restarted`,
`host.registered/online/offline`,
`healthcheck.passed/failed`, `worker.updated`, `secret.updated`.

## Misc

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/health` | none | `{ok: true, version, time}` |

## Errors

All failures return `{ "error": { "code": "...", "message": "..." } }`.
Codes: `bad_request`, `unauthorized`, `forbidden`, `not_found`, `conflict`
(idempotency key reused with a different payload), `unprocessable`
(manifest/validation failure), `rate_limited`.
