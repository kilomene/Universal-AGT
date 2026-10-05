# Universal AGT — Wire Protocol (v1)

Language-independent contract between **agents**, the **control plane**, and the
**persistent host worker**. Every SDK, the CLI, the dashboard, and the worker
implement exactly this. The protocol is REST + JSON over HTTPS, with one
Server-Sent Events (SSE) stream for live events.

Base URL: `https://<control-plane-host>/v1`
Content-Type: `application/json` everywhere unless noted.

---

## Changelog

- **2026-10-05 — Phase 4 (multi-application hosting).**
  - Compose deployments now get the same host-port safety as `docker run`:
    before `docker compose up` the worker reads the declared published
    ports via `docker compose config --format json` and verifies each free
    at OS level (bind test) and Docker level (`docker ps` scan); ports
    already held by the same compose project's current stack are skipped
    (a redeploy replaces its own stack). A collision fails the deploy task
    before any container starts.
  - Compose rollback fixed: on healthcheck failure the worker tears down
    the new unhealthy stack (`docker compose down`) and restores the
    previous stack (`docker compose up` with the compose file recorded in
    its state). Worker state now records `compose_project`,
    `compose_file`, and `image_built` per deployment.
  - Garbage collection: after every successful deploy the worker keeps
    `DEPLOY_KEEP_GENERATIONS` (default 2) newest generations per project
    and removes older stopped containers (`docker rm`) plus images the
    worker itself built (`docker rmi`), never an image still referenced by
    a kept generation, never a prebuilt/external image, and never a
    container docker still reports as running. Compose generations are
    collected with `compose down`.
  - Compose port discovery no longer parses `docker ps` text: it reads
    `docker compose -p <name> ps --format json` (Publishers) with a
    `docker inspect` NetworkSettings.Ports fallback.
  - Artifact extraction now detects tar/zip by magic bytes instead of the
    download filename (the worker stores artifacts as `<id>.bin`).
  - No REST changes: all of the above is worker behavior; the API surface
    is backwards compatible.

- **2026-10-05 — Phase 3 (task/deployment reliability).**
  - Task retry policy: a worker-reported `failed` now becomes `retrying`
    (then back to `queued` via the sweeper) when the retry budget
    (`attempts`/`max_attempts`) allows and the task type is safe to re-run
    from where it failed (idempotent reads always; `deploy`/`restart`/
    `start`/`stop`/build tasks only when the attempt never reached
    `running`; `remove`/`rollback` never). `retrying` is now a reachable
    state (`claimed|running → retrying → queued`). Events: `task.retrying`,
    `task.requeued`.
  - Claim leases: `POST /v1/worker/tasks/claim` records
    `lease_expires_at` (now + `TASK_CLAIM_LEASE_S`, default 600s), refreshed
    on every progress report. The stuck-task sweeper requeues tasks stuck in
    `claimed`/`running` past their lease (or fails them when the retry budget
    is exhausted), and moves `retrying → queued`. Tasks expose
    `lease_expires_at` / `last_progress_at`.
  - `POST /v1/deployments/:id/rollback` no longer inserts a duplicate
    deployment row (which 500'd on the unique constraint). It now creates a
    `type=rollback` task (`payload: {deployment_id, target_deployment_id}`)
    against the last healthy deployment of the same project+host, emits
    `deployment.rollback_requested`, and the worker's completion report
    drives `deployment.rolled_back`. Response shape is unchanged
    (`{deployment, task}`); `deployment` is the deployment being rolled back.
  - `POST /v1/deployments` accepts an optional `host_port` (integer
    1–65535, requires `host_id`): reserved in the new `port_allocations`
    registry (409 on collision). The worker verifies the port is free at OS
    level (bind test) and Docker level (`docker ps` scan) before
    `docker run`, failing the task with a clear error on collision.
    `deployments.ports` is now written from the deploy task's `result.ports`
    on completion. Reservations release when the deployment reaches
    `failed`/`rolled_back`/`stopped` or is superseded.
  - Deployment idempotency now compares the `mode`/`artifact_id` columns
    (previously read from a row that lacked them → false 409s).
  - Container names are unique per deployment attempt
    (`uaht-<project>-<version>-<deployment[:8]>`): same-version redeploys no
    longer stop/remove the live container before health passes.
  - **Security tightening (intentional):** `POST /v1/tasks` enforces a
    per-type permission map — `deploy`/`remove`/`rollback`/`build`/
    `docker-*`/`environment-update`/`artifact-upload` need `deploy`;
    `restart`/`start` need `restart`; `stop` needs `stop`; reads
    (`logs`/`status`/`healthcheck`/`system-info`/`artifact-download`) need
    `read_status`. `POST /v1/tasks/:id/cancel` now needs the `deploy`
    permission or task creatorship (was `read_status` alone).

---

## 1. Authentication

Two credential kinds, both sent as `Authorization: Bearer <token>`:

| Kind | Issued by | Used by | Identifies |
|------|-----------|---------|------------|
| Agent API key | `POST /v1/agents/register` (one-time, shown once) | agents, SDKs, CLI | an agent row; carries scoped `permissions` |
| Host token | `POST /v1/hosts/register` (one-time, shown once) | host worker | a host row |

Tokens are stored as SHA-256 hashes server-side. Agents and hosts are
distinguished by which endpoints accept their token (see §3). A host token can
never call agent endpoints and vice versa.

Agent permissions (subset, enforced per endpoint):
`deploy`, `read_status`, `read_logs`, `restart`, `stop`, `remove`,
`manage_domains`, `approve_deployments`, `manage_secrets`

Error shape (all failures):

```json
{ "error": { "code": "forbidden", "message": "missing permission: deploy" } }
```

Common codes: `bad_request`, `unauthorized`, `forbidden`, `not_found`,
`conflict` (idempotency key reused with *different* payload),
`unprocessable` (manifest/validation failure), `rate_limited`.

---

## 2. Conventions

- IDs are UUID strings. Event IDs are integers (bigserial).
- Timestamps are ISO-8601 UTC.
- Pagination: `?limit=` (default 50, max 500), `?cursor=` (opaque), `?since=` (ISO time, events only).
- **Idempotency:** `POST /v1/tasks`, `POST /v1/deployments` accept an
  `idempotency_key` in the JSON body. Re-sending the *same* key with an
  *equal* payload returns the original object with HTTP 200 and
  `"idempotent_replay": true`. Same key with a *different* payload → HTTP 409.
- Long-polling: worker endpoints accept `?wait=` (seconds, max 30). The server
  holds the request until work appears or the timeout elapses, then returns.
- No IP addresses appear anywhere in requests or responses. Hosts are
  referenced by `host_id` / `host_name` only.

---

## 3. Endpoints

### 3.1 Agents (agent API key)

```
POST /v1/agents/register        {name, type?, capabilities?[], permissions?{}}
                                → 201 {agent:{...}, api_key:"<show once>"}
GET  /v1/agents/me              → caller's own agent row (no secret fields)
```

### 3.2 Tasks — the durable work queue (agent API key)

```
POST /v1/tasks
  {
    "type": "deploy" | "restart" | "stop" | "start" | "remove" | "rollback" |
            "logs" | "status" | "healthcheck" | "build" | "docker-build" |
            "docker-run" | "docker-compose" | "environment-update" |
            "artifact-download" | "artifact-upload" | "system-info",
    "payload": { ... },            # type-specific, see §5
    "idempotency_key": "uuid?",    # optional, strongly recommended
    "priority": 0,                 # higher = sooner
    "host_id": "uuid?",            # optional target host; else any capable host
    "mode": "automatic" | "manual" # default automatic; manual => awaiting_approval
  }
  → 201 {task:{...}}  (or 200 + idempotent_replay on key reuse)

GET  /v1/tasks/:id               → {task:{...}}
GET  /v1/tasks?status=&type=&host_id=&limit=&cursor=
POST /v1/tasks/:id/cancel        → terminal state 'cancelled' (if not terminal);
                                   needs `deploy` permission or task creatorship
POST /v1/tasks/:id/approve       # needs approve_deployments; awaiting_approval → queued
POST /v1/tasks/:id/reject        # needs approve_deployments; awaiting_approval → cancelled
```

`POST /v1/tasks` enforces a per-type permission map (see Changelog
2026-10-05): `deploy`/`remove`/`rollback`/`build`/`docker-build`/
`docker-run`/`docker-compose`/`environment-update`/`artifact-upload` need
`deploy`; `restart`/`start` need `restart`; `stop` needs `stop`;
`logs`/`status`/`healthcheck`/`system-info`/`artifact-download` need
`read_status`.

Task object:

```json
{
  "id": "uuid", "type": "deploy", "status": "running",
  "priority": 0, "payload": {...}, "result": {...}, "error": null,
  "attempts": 1, "max_attempts": 3,
  "created_by": "uuid", "assigned_to": "uuid", "claimed_by": "uuid",
  "created_at": "...", "started_at": "...", "completed_at": null
}
```

Status machine: `queued → claimed → running → completed|failed`
(`running → awaiting_approval → queued|cancelled` in manual mode;
`cancelled` reachable from any non-terminal state;
`claimed|running → retrying → queued` when a failure is retryable — see
Changelog 2026-10-05.)

**Retry rule:** a worker-reported `failed` becomes `retrying` (sweeper moves
it back to `queued`) when `attempts < max_attempts` and the task type is
safe to re-run: idempotent reads always; `deploy`/`restart`/`start`/`stop`
and build tasks only when the attempt never reached `running`;
`remove`/`rollback` never auto-retry.

**Resumability rule:** the task row is the source of truth. If the creating
agent vanishes, the worker still executes the queued task, stores the result,
and the agent reads it later with `GET /v1/tasks/:id`.

### 3.3 Worker-facing (host token only)

```
POST /v1/hosts/register
  {name, host_type?, capabilities?[], worker_version?}
  → 201 {host:{...}, host_token:"<show once>"}

POST /v1/hosts/:id/heartbeat
  {cpu_pct, ram_pct, disk_pct, docker_status, running_apps:[...],
   worker_version?, total_cpu?, total_ram_mb?, total_disk_gb?}
  → 200 {host:{...}, pending_tasks: n}

POST /v1/worker/tasks/claim?wait=25
  {host_id, capabilities?[]}
  → 200 {task:{...}} | 204 (nothing within wait window)

POST /v1/worker/tasks/:id/progress
  {status: "claimed"|"running"|"awaiting_approval"|"completed"|"failed",
   log_chunk?: "text appended to task logs",
   result?: {...}, error?: "message"}
  → 200 {task:{...}}
```

Claim semantics: exactly one host gets a queued task (atomic `UPDATE ...
WHERE status='queued'` with `RETURNING`; losers get 204). This is what makes
multi-host safe without a lock service.

### 3.4 Hosts (agent API key, read_status)

```
GET /v1/hosts
GET /v1/hosts/:id        → includes last heartbeat stats + running_apps
```

### 3.5 Projects & artifacts (agent API key)

```
POST /v1/projects   {name, owner?, repository?, runtime?, configuration?}
GET  /v1/projects   GET /v1/projects/:id
PUT  /v1/projects/:id            # update configuration (validated manifest)

POST /v1/artifacts/init
  {project_id, filename, size, checksum:"sha256:<hex>", version}
  → 201 {artifact:{...}, upload_url:"/v1/artifacts/:id/content"}
PUT  /v1/artifacts/:id/content   # Content-Type: application/octet-stream
                                 # server verifies size + sha256, rejects mismatch
GET  /v1/artifacts/:id
GET  /v1/artifacts?project_id=
GET  /v1/artifacts/:id/download  # host token OR agent token; streams bytes
```

### 3.6 Deployments (agent API key)

```
POST /v1/deployments
  {project_id, host_id?, version, artifact_id?, mode?:"automatic"|"manual",
   host_port?: 1-65535,   # optional fixed host port; reserved in the port
                          # registry (requires host_id; 409 on collision).
                          # The worker verifies it is free before `docker run`.
   idempotency_key?}
  → creates a task type=deploy (+ a deployment row); 201 {deployment, task}

GET  /v1/deployments?project_id=&host_id=&status=
GET  /v1/deployments/:id         → {deployment, task?}
POST /v1/deployments/:id/rollback  # needs deploy; creates a type=rollback task
                                   # against the previous healthy deployment
                                   # (no new deployment row); emits
                                   # deployment.rollback_requested

GET  /v1/services?host_id=       → running deployments (friendly view)
POST /v1/services/:id/restart    # needs restart; creates task type=restart
POST /v1/services/:id/stop       # needs stop
POST /v1/services/:id/start      # needs restart
```

Deployment object:

```json
{
  "id":"uuid","project_id":"uuid","host_id":"uuid","version":"1.0.0",
  "status":"running",            # requested|approved|building|starting|
                                 # healthcheck|running|failed|rolled_back|stopped|stopping
  "container_ids":[],"ports":{},"domains":[],
  "health_status":"healthy", "rollback_of": null
}
```

### 3.7 Secrets (agent API key, manage_secrets)

```
POST /v1/projects/:id/secrets   {name, value}   # encrypted at rest, never returned
GET  /v1/projects/:id/secrets   → [{name, created_at}]   # names only
DELETE /v1/projects/:id/secrets/:name
```

The worker receives decrypted values only inside task payloads for deployments
it is executing, over its authenticated channel. Secrets never appear in
logs, events, or task results.

### 3.8 Events — append-only journal (agent API key, read_status)

```
GET /v1/events?type=&since=&limit=&cursor=   → {events:[...], next_cursor}
GET /v1/events/stream                        # SSE: "data: {event}\n\n" per event
```

Event object:

```json
{"id": 1234, "type": "deployment.completed", "actor_type": "host",
 "actor_id": "persistent-host-01", "task_id": "uuid",
 "deployment_id": "uuid", "host_id": "uuid",
 "payload": {...}, "created_at": "..."}
```

Canonical event types: `agent.connected`, `agent.disconnected`,
`task.created`, `task.claimed`, `task.started`, `task.awaiting_approval`,
`task.approved`, `task.rejected`, `task.completed`, `task.failed`,
`task.cancelled`, `artifact.created`, `artifact.upload_failed`,
`deployment.requested`, `deployment.approved`, `deployment.started`,
`deployment.building`, `deployment.healthcheck`, `deployment.completed`,
`deployment.failed`, `deployment.rolled_back`, `service.started`,
`service.stopped`, `service.restarted`, `host.registered`, `host.online`,
`host.offline`, `healthcheck.passed`, `healthcheck.failed`,
`worker.updated`, `secret.updated`.

### 3.9 Misc

```
GET /v1/health → {ok:true, version, time}
```

### 3.10 Domains — optional Cloudflare DNS (agent API key, manage_domains)

```
POST /v1/domains   {deployment_id, hostname}
                   → 201 {domain:{hostname, cf_record_id?, status, added_at}}
                   # status: 'active' (DNS created) | 'dns_pending' (Cloudflare
                   # not configured — metadata only) | 'error'
GET  /v1/domains?deployment_id=   → {domains:[...]}
DELETE /v1/domains {deployment_id, hostname} → removes + deletes the DNS record
```

Architectural rule: the system never stores host IPs, so A-records pointing
at a host are intentionally unsupported. When `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ZONE_ID`, and `PUBLIC_INGRESS_HOSTNAME` are all set, the control
plane creates a proxied CNAME `hostname → PUBLIC_INGRESS_HOSTNAME` (the
ingress is operator-run: a tunnel, reverse proxy, or load balancer in front
of the host). Otherwise the domain is kept as metadata and DNS is done
manually. Use a Cloudflare token scoped to "Zone / DNS / Edit" on the zone.

### 3.11 Misc

```
GET /v1/health → {ok:true, version, time}
```

---

## 4. `agent.deploy.json` manifest

Projects may ship an `agent.deploy.json` at the artifact root. The worker
validates it before any build; invalid → task fails fast, no partial state.

```json
{
  "name": "my-api",
  "runtime": "docker",
  "build": { "dockerfile": "Dockerfile", "context": "." },
  "service": { "port": 3000, "healthcheck": "/health" },
  "resources": { "memory": "1g", "cpu": "1" },
  "restart": "unless-stopped",
  "env": { "NODE_ENV": "production" }
}
```

Validation rules: `name` required (must match project name); `runtime` in
`docker|docker-compose|static`; `service.port` 1–65535; `resources.memory`
like `256m|1g`; `resources.cpu` positive number; `restart` in
`no|always|unless-stopped|on-failure`.

---

## 5. Task payloads by type

- `deploy`: `{project_id, version, artifact_id?, manifest?}` (+ secrets injected server-side)
- `rollback`: `{deployment_id, target_deployment_id?}` — the control plane
  sets `target_deployment_id` to the last healthy deployment of the same
  project+host; a hand-built task may omit it (worker picks the newest
  restorable local deployment)
- `restart|stop|start|remove|logs|status|healthcheck`: `{deployment_id}`
- `build|docker-build`: `{project_id, artifact_id?, dockerfile?, context?}`
- `docker-run|docker-compose`: `{project_id, compose_file?, service?}`
- `environment-update`: `{deployment_id, env:{...}}`
- `artifact-download|artifact-upload`: `{artifact_id, destination?}`
- `system-info`: `{}`

Task `result` for `deploy`: `{deployment_id, status, health_status, ports}`.
For `logs`: `{logs: "..."}`. For `system-info`: `{cpu, ram, disk, docker, ...}`.

---

## 6. Security notes

- HTTPS only in production; tokens are bearer secrets.
- The worker enforces a command **allowlist**: it only ever runs Docker and a
  fixed set of inspection commands derived from task fields — never a raw
  shell string from the network.
- Artifact SHA-256 is verified by the API on upload AND by the worker after
  download. Mismatch → task failed, artifact quarantined.
- Rate limiting: 120 req/min per agent key, 600 req/min per host token
  (heartbeat/claim heavy by design).
