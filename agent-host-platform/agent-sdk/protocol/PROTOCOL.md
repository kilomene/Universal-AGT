# Universal AGT — Wire Protocol (v1)

Language-independent contract between **agents**, the **control plane**, and the
**persistent host worker**. Every SDK, the CLI, the dashboard, and the worker
implement exactly this. The protocol is REST + JSON over HTTPS, with one
Server-Sent Events (SSE) stream for live events.

Base URL: `https://<control-plane-host>/v1`
Content-Type: `application/json` everywhere unless noted.

---

## Changelog

- **2026-10-05 — W15 contract audit (doc corrections only; no wire changes).**
  - **Permissions:** §1 listed `read_logs` and `remove` — those permissions do
    not exist. The real set is `deploy`, `read_status`, `restart`, `stop`,
    `manage_secrets`, `manage_domains`, `approve_deployments` (per
    `middleware/auth.ts`).
  - **Task approve side effect:** `POST /v1/tasks/:id/approve` also flips the
    linked deployment to `approved` and emits `deployment.approved`
    (previously undocumented).
  - **Claim body:** `POST /v1/worker/tasks/claim` takes `{host_id}` only —
    capabilities are read from the host row, not the claim body.
  - **Artifacts:** `POST /v1/artifacts/init` and `PUT /v1/artifacts/:id/content`
    require the `deploy` permission (not just any agent key); permission
    annotations added to projects endpoints too (`POST`/`PUT` need `deploy`,
    reads need `read_status`).
  - **Deployments:** `GET /v1/deployments` supports `?limit=` (was
    undocumented); added the missing host-token endpoint
    `POST /v1/deployments/:id/settle-rollback-ports`
    `{target_deployment_id}` → `deployment.ports_settled` (called by the
    worker after a completed rollback).
  - **Events:** canonical list replaced with the 41 types the server actually
    emits (16 missing ones added: `agent.key_rotated`, `task.retrying`,
    `task.requeued`, `deployment.rollback_requested`,
    `deployment.rollback_failed`, `deployment.ports_settled`,
    `service.crash_loop`, `service.removed`, `host.token_rotated`,
    `host.degraded`, `domain.requested`, `domain.active`, `domain.failed`,
    `domain.removed`, `domain.remove_failed`; 6 never-emitted ones removed:
    `agent.disconnected`, `deployment.building`, `deployment.healthcheck`,
    `healthcheck.passed`, `healthcheck.failed`, `worker.updated`).
  - **Cursor semantics:** the events cursor is an integer event id, not an
    opaque token (only the tasks cursor is opaque base64url).
  - **Domains:** lifecycle corrected to `requested → configuring →
    active | failed`, `removing → removed` on detach (migration 007 supersedes
    the Phase 7 `dns_pending`/`error` names); added
    `GET /v1/domains/:hostname`.
- **2026-10-05 — Phase 9 (end-to-end testing).**
  - **Additive (backwards compatible):** `POST /v1/deployments` now injects
    `artifact_checksum` + `artifact_size` into the `type=deploy` task payload
    from the verified artifact row (422 when the artifact has no verified
    checksum). Previously the worker refused every artifact-based deploy
    with "no artifact_checksum in payload" because the task payload never
    carried it — the E2E suite caught the wiring gap.
  - **Worker fix (no protocol change):** the dispatcher no longer re-reports
    `claimed` after a successful claim — the claim endpoint already moved
    `queued → claimed` atomically, and re-reporting was a `claimed →
    claimed` self-transition the state machine rejects (409), which failed
    every dispatched task. First progress report is now `running`.
- **2026-10-05 — Phase 7 (public ingress, honest architecture).**
  - **The honest problem (spec §28):** the persistent host is outbound-only,
    so DNS CNAME records alone CANNOT make it publicly reachable. Phase 7
    adds an optional, provider-neutral, disabled-by-default ingress layer
    instead of pretending DNS is enough. Three modes, documented in
    `docs/cloudflare.md`: (a) metadata-only (default today — domains
    recorded, `dns_pending`), (b) direct (operator runs their own ingress;
    their responsibility), (c) cloudflare-tunnel (worker-managed,
    outbound-only, recommended for the private-host case). Without (b) or
    (c), domains resolve but traffic cannot reach the host — stated plainly
    in the docs and here.
  - **Additive (backwards compatible):**
    - New task type `ingress-sync` (now 18 protocol types): `{}` payload;
      the worker rebuilds its ingress route table from the current
      deployments' domains (`GET /v1/worker/domains`, host token) joined
      with local container host ports, rewrites the provider's `config.yml`,
      and restarts the tunnel process. Idempotent: classified `safe` for
      retry, and safe to trigger manually. Needs the `deploy` permission.
      The worker also runs a best-effort in-process sync after successful
      `deploy`/`remove` tasks when ingress is enabled.
    - `POST /v1/domains` accepts optional `ingress: 'tunnel' | 'direct'`
      (default: tunnel when `TUNNEL_INGRESS_HOSTNAME` is set on the control
      plane, else direct — the previous behavior). Tunnel mode creates the
      CNAME `hostname → TUNNEL_INGRESS_HOSTNAME`
      (e.g. `<tunnel-id>.cfargotunnel.com`) instead of
      `→ PUBLIC_INGRESS_HOSTNAME`, stores `ingress` + `tunnel_host` on the
      domain entry, and queues an `ingress-sync` task for the host on
      add/remove. Requesting tunnel mode without `TUNNEL_INGRESS_HOSTNAME`
      configured is a 422 with a clear message. Garbage `ingress` values
      are 400. Domain entries recorded before Phase 7 read as
      `ingress: 'direct'`.
    - New host-token endpoint `GET /v1/worker/domains` →
      `{domains: [{deployment_id, hostname, ingress}]}` for the host's
      non-terminal deployments.
    - Worker config (all optional, off by default): `WORKER_INGRESS_ENABLED`,
      `WORKER_INGRESS_PROVIDER` (`cloudflare-tunnel`), and the tunnel token
      via `WORKER_TUNNEL_TOKEN` env (never in the repo, never logged).
  - Worker-side (no other REST changes): new `host-worker/ingress/` package
    with the provider-neutral `IngressProvider` interface (`name`,
    `setup`, `add_route`, `remove_route`, `sync_routes`, `status`,
    `shutdown` — loopback-only targets enforced) and a `cloudflare-tunnel`
    provider that provisions a SHA-256-pinned `cloudflared` release
    (2026.10.0; official per-asset digests, amd64 re-verified by download —
    never auto-trusts "latest"), runs
    `cloudflared tunnel --token <TOKEN> run` as a supervised subprocess
    (crash restart with backoff; argv-only, `shell=False`), and applies
    routes as `hostname → http://127.0.0.1:<port>` in `config.yml` with a
    catch-all 404. A failed download or missing token logs clearly and
    leaves ingress disabled; the worker keeps running. Honesty note kept in
    code and docs: for dashboard-created (token) tunnels, Cloudflare reads
    public-hostname routes from the tunnel's dashboard configuration, not
    from `config.yml` — the worker-generated file is the canonical route
    table to mirror there (and is used verbatim by locally-managed
    tunnels).
  - SDKs/CLI (additive): `add_domain(..., ingress=)` / `addDomain(...,
    ingress)` and `agent-host domains add --ingress tunnel|direct`.
  - What still needs a human: creating the tunnel in the Cloudflare
    dashboard, copying the tunnel token + `<tunnel-id>.cfargotunnel.com`
    hostname, and adding each public hostname in the tunnel's dashboard
    **Public hostnames** tab (required for token tunnels). Nothing in this
    phase can do those steps.

- **2026-10-05 — Phase 6 (security hardening).**
  - **Intentional tightenings (behavior changes):**
    - `docker-run` payload field `extra_args` **removed** — it allowed
      arbitrary `docker run` flags (`--privileged`, `-v /:/host`) straight
      to host root. The worker now rejects any `docker-run` task carrying
      it (task reported `failed`, nothing executed). Remove the field from
      payloads and re-submit.
    - `POST /v1/agents/register` is gated by a provisioning token:
      header `X-Provisioning-Token: <UAHT_PROVISIONING_TOKEN>` (env, required
      at startup when `NODE_ENV=production`). With the env unset, only the
      very first registration is open (bootstrap); afterwards registration
      is closed until the operator sets the token. Unauthenticated requests
      are now rate-limited (10/min per IP, `RATE_LIMIT_UNAUTH_PER_MIN`).
    - Artifact uploads are capped: `ARTIFACT_MAX_BYTES` (default 500MB) is
      enforced on `Content-Length` upfront, per-chunk while streaming, and
      on the declared `size` at `POST /v1/artifacts/init` — all 413
      `payload_too_large` on exceed (new error code).
    - `GET /v1/artifacts/:id/download` is scoped: a host token may only
      download artifacts referenced by tasks it claimed (`payload.artifact_id`);
      other hosts get 403. Agent `read_status` access is unchanged.
    - `?api_key=` query fallback is now honored **only** on
      `GET /v1/events/stream` (EventSource can't set headers); every other
      endpoint requires the `Authorization` header.
  - **Additive (backwards compatible):**
    - `POST /v1/agents/me/rotate` (agent auth) — returns a NEW API key,
      invalidates the old one immediately, emits `agent.key_rotated`.
    - `POST /v1/hosts/:id/rotate-token` (host auth, `:id` must match the
      token's host) — returns a NEW host token, invalidates the old one,
      emits `host.token_rotated`.
    - `GET /v1/worker/projects/:project_id/secrets` (host token) — returns
      the project's DECRYPTED secrets, but only when the host has a
      claimed/active task or a live deployment for that project (403
      otherwise). This completes the secrets feature: the worker pulls
      secrets at deploy time and injects them as container env (payload
      secrets still win on collision; never persisted to state.json).
    - New error code `payload_too_large` (HTTP 413).
  - Worker-side hardening (no REST changes): tar extraction uses
    `tarfile.data_filter` (blocks symlink escapes); manifest-controlled
    paths (`build.dockerfile`, `build.context`, `compose_file`) are confined
    to the extracted artifact tree; `artifact-upload` refuses to read
    anything under the worker's `config/` directory (worker.env); the
    self-updater now health-gates the restarted worker (fresh boot marker +
    active unit within `WORKER_UPDATE_HEALTH_TIMEOUT_S`, default 120s) and
    rolls back on failure; `agent-host-worker --self-check` runs startup
    self-checks without starting the loop.
  - Database: migration `005_events_truncate_block.sql` adds an event
    trigger aborting any `TRUNCATE` on the append-only `events` table
    (row-level triggers can't block TRUNCATE; needs superuser to install).
  - Accepted limitation (documented, not fixed): no replay protection on
    bearer tokens — static tokens + rotation + TLS are the mitigations;
    HMAC request signing is future work.

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

Agent permissions (enforced per endpoint; the complete set — an agent row's
`permissions` JSONB carries a subset of these):
`deploy`, `read_status`, `restart`, `stop`, `manage_secrets`,
`manage_domains`, `approve_deployments`

Error shape (all failures):

```json
{ "error": { "code": "forbidden", "message": "missing permission: deploy" } }
```

Common codes: `bad_request`, `unauthorized`, `forbidden`, `not_found`,
`conflict` (idempotency key reused with *different* payload),
`unprocessable` (manifest/validation failure), `payload_too_large` (HTTP 413),
`rate_limited`.

---

## 2. Conventions

- IDs are UUID strings. Event IDs are integers (bigserial).
- Timestamps are ISO-8601 UTC.
- Pagination: `?limit=` (default 50, max 500), `?cursor=`, `?since=` (ISO time,
  events only). Cursor semantics differ per endpoint: the events cursor is an
  integer event id (`next_cursor` from the previous page); the tasks cursor
  is an opaque base64url token. Never invent a cursor value — always pass
  through the server's `next_cursor`.
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
                                # gated: X-Provisioning-Token header required
                                # (UAHT_PROVISIONING_TOKEN), except the very
                                # first registration when the env is unset
                                # (bootstrap mode)
GET  /v1/agents/me              → caller's own agent row (no secret fields)
POST /v1/agents/me/rotate       → 200 {api_key:"<new show-once key>"}
                                # old key invalidated immediately
```

### 3.2 Tasks — the durable work queue (agent API key)

```
POST /v1/tasks
  {
    "type": "deploy" | "restart" | "stop" | "start" | "remove" | "rollback" |
            "logs" | "status" | "healthcheck" | "build" | "docker-build" |
            "docker-run" | "docker-compose" | "environment-update" |
            "artifact-download" | "artifact-upload" | "system-info" |
            "ingress-sync",
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
POST /v1/tasks/:id/approve       # needs approve_deployments; awaiting_approval → queued;
                                   also flips the task's linked deployment to
                                   'approved' and emits deployment.approved
POST /v1/tasks/:id/reject        # needs approve_deployments; awaiting_approval → cancelled
```

`POST /v1/tasks` enforces a per-type permission map (see Changelog
2026-10-05): `deploy`/`remove`/`rollback`/`build`/`docker-build`/
`docker-run`/`docker-compose`/`environment-update`/`artifact-upload`/
`ingress-sync` need `deploy`; `restart`/`start` need `restart`; `stop` needs
`stop`; `logs`/`status`/`healthcheck`/`system-info`/`artifact-download` need
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
safe to re-run: idempotent reads always; `ingress-sync` (reconciles the
tunnel route table to the desired state — re-running is a no-op when
nothing changed); `deploy`/`restart`/`start`/`stop`
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
  {host_id}
  → 200 {task:{...}} | 204 (nothing within wait window)
  # capabilities are read from the host row, not the claim body

POST /v1/worker/tasks/:id/progress
  {status: "claimed"|"running"|"awaiting_approval"|"completed"|"failed",
   log_chunk?: "text appended to task logs",
   result?: {...}, error?: "message"}
  → 200 {task:{...}}

GET /v1/worker/projects/:project_id/secrets
  → 200 {secrets: {NAME: "value", ...}}   # decrypted; only when this host
                                         # has a claimed/active task or live
                                         # deployment for the project (else 403)

GET /v1/worker/domains
  → 200 {domains: [{deployment_id, hostname, ingress}]}  # every domain entry
                                         # on this host's non-terminal
                                         # deployments; drives the worker's
                                         # ingress route sync (Phase 7)
```

Claim semantics: exactly one host gets a queued task (atomic `UPDATE ...
WHERE status='queued'` with `RETURNING`; losers get 204). This is what makes
multi-host safe without a lock service.

### 3.4 Hosts (agent API key, read_status)

```
GET /v1/hosts
GET /v1/hosts/:id        → includes last heartbeat stats + running_apps
POST /v1/hosts/:id/rotate-token   # host token only, :id must match the token's
                                # host → 200 {host_token:"<new show-once token>"}
```

### 3.5 Projects & artifacts (agent API key)

```
POST /v1/projects   {name, owner?, repository?, runtime?, configuration?}
                     # needs deploy
GET  /v1/projects   GET /v1/projects/:id   # needs read_status
PUT  /v1/projects/:id            # needs deploy; accepts {configuration?,
                                 # repository?, runtime?} (validated manifest)

POST /v1/artifacts/init           # needs deploy
  {project_id, filename, size, checksum:"sha256:<hex>", version}
  → 201 {artifact:{...}, upload_url:"/v1/artifacts/:id/content"}
  # 413 payload_too_large when size > ARTIFACT_MAX_BYTES (default 500MB)
PUT  /v1/artifacts/:id/content   # needs deploy; Content-Type: application/octet-stream
                                 # server verifies size + sha256, rejects mismatch
                                 # 413 when Content-Length or the streamed body
                                 # exceeds ARTIFACT_MAX_BYTES
GET  /v1/artifacts/:id           # needs read_status
GET  /v1/artifacts?project_id=   # needs read_status
GET  /v1/artifacts/:id/download  # host token (scoped: only artifacts from
                                 # tasks the host claimed) OR agent token
                                 # with read_status; streams bytes
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

GET  /v1/deployments?project_id=&host_id=&status=&limit=
GET  /v1/deployments/:id         → {deployment, task?}
POST /v1/deployments/:id/rollback  # needs deploy; creates a type=rollback task
                                   # against the previous healthy deployment
                                   # (no new deployment row); emits
                                   # deployment.rollback_requested
POST /v1/deployments/:id/settle-rollback-ports  # host token only (host must
                                   # own the deployment); body
                                   # {target_deployment_id}. Called by the
                                   # worker after a completed rollback to
                                   # release the rolled-back deployment's
                                   # port reservations and re-reserve the
                                   # target's host ports; emits
                                   # deployment.ports_settled

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
it is executing, over its authenticated channel — or by pulling
`GET /v1/worker/projects/:project_id/secrets` (host token, scoped to projects
with live work on that host) at deploy time. Secrets never appear in
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

Canonical event types (41 — every type the server emits, verified against
`routes/` + `lib/` 2026-10-05):

- Agents: `agent.connected`, `agent.key_rotated`
- Tasks: `task.created`, `task.claimed`, `task.started`, `task.retrying`,
  `task.awaiting_approval`, `task.approved`, `task.rejected`,
  `task.completed`, `task.failed`, `task.cancelled`, `task.requeued`
- Artifacts: `artifact.created`, `artifact.upload_failed`
- Deployments: `deployment.requested`, `deployment.approved`,
  `deployment.started`, `deployment.completed`, `deployment.failed`,
  `deployment.rolled_back`, `deployment.rollback_requested`,
  `deployment.rollback_failed`, `deployment.ports_settled`
- Services: `service.started`, `service.stopped`, `service.restarted`,
  `service.removed`, `service.crash_loop`
- Hosts: `host.registered`, `host.online`, `host.offline`, `host.degraded`,
  `host.token_rotated`
- Secrets: `secret.changed`, `secret.deleted`
- Domains: `domain.requested`, `domain.active`, `domain.failed`,
  `domain.removed`, `domain.remove_failed`

### 3.9 Misc

```
GET /v1/health → {ok:true, version, time}
```

### 3.10 Domains — optional Cloudflare DNS (agent API key, manage_domains)

```
POST /v1/domains   {deployment_id, hostname, ingress?}
                   → 201 {domain:{hostname, cf_record_id?, status, added_at,
                                 ingress, tunnel_host?}}
                   # lifecycle (migration 007): requested → configuring →
                   # active | failed; DELETE moves removing → removed.
                   # The control plane configures the DNS record and the
                   # tunnel/direct route, then marks 'active' (or 'failed'
                   # with an error, retryable). 'requested' is the initial
                   # state when Cloudflare is not configured — metadata
                   # only until the operator acts.
                   # ingress: 'tunnel' | 'direct' (default: tunnel when
                   # TUNNEL_INGRESS_HOSTNAME is set on the control plane,
                   # else direct). Tunnel mode without TUNNEL_INGRESS_HOSTNAME
                   # is 422. Tunnel-mode changes queue an `ingress-sync` task
                   # for the host.
GET  /v1/domains?deployment_id=   → {domains:[...]}
GET  /v1/domains/:hostname        → {domain:{...}} (live rows only)
DELETE /v1/domains {deployment_id, hostname} → removes + deletes the DNS record
                                             (removing → removed)
```

Architectural rule: the system never stores host IPs, so A-records pointing
at a host are intentionally unsupported. When `CLOUDFLARE_API_TOKEN`,
`CLOUDFLARE_ZONE_ID`, and an ingress hostname are set, the control plane
creates a proxied CNAME:

- `direct` mode (default unless tunnel is configured):
  `hostname → PUBLIC_INGRESS_HOSTNAME`. The ingress is operator-run (a
  reverse proxy, load balancer, or tunnel they manage in front of the
  host); reaching the host is the operator's responsibility.
- `tunnel` mode: `hostname → TUNNEL_INGRESS_HOSTNAME`
  (e.g. `<tunnel-id>.cfargotunnel.com`). The host runs a worker-managed
  `cloudflared` tunnel (outbound-only); the worker syncs
  `hostname → http://127.0.0.1:<container-port>` routes via the
  `ingress-sync` task type.

Otherwise the domain is kept as metadata and DNS is done manually. Honest
limitation (spec §28): DNS alone never makes an outbound-only host
reachable — without a working (b) direct or (c) tunnel ingress, domains
resolve but traffic cannot reach the host. Use a Cloudflare token scoped to
"Zone / DNS / Edit" on the zone.

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

- `deploy`: `{project_id, project_name, host_id, version, artifact_id?,
  artifact_checksum?, artifact_size?, requested_host_port?, deployment_id,
  manifest?}` — the control plane injects `project_name` (from the project
  row) and `artifact_checksum`/`artifact_size` (from the verified artifact
  row) on `POST /v1/deployments` (2026-10-05); the worker refuses artifact
  deploys without `artifact_checksum` (+ secrets injected server-side)
- `rollback`: `{deployment_id, target_deployment_id?}` — the control plane
  sets `target_deployment_id` to the last healthy deployment of the same
  project+host; a hand-built task may omit it (worker picks the newest
  restorable local deployment)
- `restart|stop|start|remove|logs|status|healthcheck`: `{deployment_id}`
- `build|docker-build`: `{project_id, artifact_id?, dockerfile?, context?}`
- `docker-run|docker-compose`: `{project_id, compose_file?, service?}`
  (`docker-run` no longer accepts `extra_args` — removed 2026-10-05; tasks
  carrying it are rejected without execution)
- `environment-update`: `{deployment_id, env:{...}}`
- `artifact-download|artifact-upload`: `{artifact_id, destination?}`
- `system-info`: `{}`
- `ingress-sync`: `{}` (empty — the sync pulls current domains from the
  control plane via `GET /v1/worker/domains` and host ports from the local
  deployment store, then reconciles the provider's route table)

Task `result` for `deploy`: `{deployment_id, status, health_status, ports}`.
For `logs`: `{logs: "..."}`. For `system-info`: `{cpu, ram, disk, docker, ...}`.
For `ingress-sync`:
`{status: "ok"|"disabled", provider, changed, routes: [{hostname,
target_host: "127.0.0.1", target_port}], skipped: [{hostname, reason}], route_count}`.

---

## 6. Security notes

- HTTPS only in production; tokens are bearer secrets.
- The worker enforces a command **allowlist**: it only ever runs Docker and a
  fixed set of inspection commands derived from task fields — never a raw
  shell string from the network.
- Artifact SHA-256 is verified by the API on upload AND by the worker after
  download. Mismatch → task failed, artifact quarantined.
- Rate limiting: 120 req/min per agent key, 600 req/min per host token
  (heartbeat/claim heavy by design), 10 req/min per IP for unauthenticated
  requests (`RATE_LIMIT_UNAUTH_PER_MIN`).
- `?api_key=` is honored only on `GET /v1/events/stream` (browser
  EventSource); all other endpoints require the `Authorization` header.
- Credential rotation: `POST /v1/agents/me/rotate`,
  `POST /v1/hosts/:id/rotate-token`. Rotate on suspected compromise; the
  old credential dies immediately.
- Accepted limitation: bearer tokens have no replay protection (no
  per-request signatures). Mitigations are TLS-everywhere, rotation, and
  short-lived operational use; HMAC request signing is future work.
