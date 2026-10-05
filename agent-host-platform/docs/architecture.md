# Universal AGT — Architecture

Universal AGT ("Universal Agent-to-Persistent-Host Deployment System") lets
autonomous agents build apps, package them, and deploy them to a persistent
Linux host without the agent staying online. Agents are temporary; the
**control plane is the source of truth**; the **host worker** is an
outbound-only daemon on the persistent machine that executes work and reports
back.

The canonical contract is
[`../agent-sdk/protocol/PROTOCOL.md`](../agent-sdk/protocol/PROTOCOL.md).
This document explains how the pieces fit together.

## The three layers

```
                        ┌───────────────────────────────┐
                        │        LAYER 1: AGENTS        │
                        │  agents, SDKs, CLI, humans    │
                        │  (any language, any machine,  │
                        │   may disappear at any time)  │
                        └──────────────┬────────────────┘
                                       │  HTTPS + Bearer token
                                       │  (agent API key)
                                       ▼
                        ┌───────────────────────────────┐
                        │   LAYER 2: CONTROL PLANE      │
                        │  REST API + durable state:    │
                        │   • agents, hosts, projects   │
                        │   • artifact store (sha256)   │
                        │   • deployments               │
                        │   • task queue (claim-safe)   │
                        │   • append-only event journal │
                        │   • secrets (encrypted)       │
                        │   • SSE stream /v1/events/stream
                        └──────────────┬────────────────┘
                                       │  HTTPS + Bearer token
                                       │  (host token; outbound-only)
                                       ▼
                        ┌───────────────────────────────┐
                        │    LAYER 3: HOST WORKER       │
                        │  systemd daemon on the        │
                        │  persistent host:             │
                        │   • heartbeats (stats)        │
                        │   • claims tasks (atomic)     │
                        │   • builds / runs containers  │
                        │   • streams progress + logs   │
                        │   • self-updates              │
                        └───────────────────────────────┘
```

**Layer 1 — Agents.** Any process with an agent API key: an LLM agent, the
`uagt` CLI, or a human driving the CLI. Agents *declare intent* (create a
task, register a deployment) and *read results later*. They never SSH into
the host, never touch Docker directly, and may vanish mid-deploy — the
system is designed for that.

**Layer 2 — Control plane.** The only durable component. It authenticates
agents and hosts, stores artifacts with SHA-256 verification, keeps the
deployment rows, runs the task queue with atomic claim semantics (exactly
one host gets a queued task), holds encrypted project secrets, and emits
the append-only event journal streamed over SSE. It never makes outbound
connections to hosts — hosts always dial in.

**Layer 3 — Host worker.** A small daemon installed on the persistent host
(see [`host-install.md`](host-install.md)). It opens outbound HTTPS to the
control plane, posts heartbeats with CPU/RAM/disk stats, long-polls
`POST /v1/worker/tasks/claim?wait=25` for work, executes tasks through a
command allowlist (Docker + fixed inspection commands — never a raw shell
string from the network), and posts progress/log chunks back. It keeps no
irreplaceable state: kill it and the control plane still knows everything.

## Data flow for a deploy

```
agent                                    control plane                        host worker
  │                                            │                                    │
  │  POST /v1/projects {name,...}              │                                    │
  │ ─────────────────────────────────────────▶ │                                    │
  │  POST /v1/artifacts/init {sha256,...}      │                                    │
  │ ─────────────────────────────────────────▶ │                                    │
  │  PUT  /v1/artifacts/:id/content (bytes)    │                                    │
  │ ─────────────────────────────────────────▶ │  verifies size + sha256            │
  │                                            │                                    │
  │  POST /v1/deployments                      │                                    │
  │   {project_id, version, artifact_id?,       │                                    │
  │    host_id?, mode, idempotency_key?}        │                                    │
  │ ─────────────────────────────────────────▶ │  creates deployment row (requested)  │
  │                                            │  creates task type=deploy (queued)   │
  │  ◀ 201 {deployment, task}                   │  emits deployment.requested          │
  │                                            │         task.created                 │
  │                                            │                                    │
  │                        agent may disconnect│ ◀ POST /v1/worker/tasks/claim?wait=25│
  │                        here — deploy        │ ─ 200 {task} (atomic claim)          │
  │                        continues anyway     │         task.claimed                 │
  │                                            │                                    │
  │                                            │ ◀ POST .../progress {running}        │
  │                                            │         task.started                 │
  │                                            │         deployment.building          │
  │                                            │                                    │
  │                                            │   worker: download artifact,         │
  │                                            │   verify sha256 again, validate      │
  │                                            │   agent.deploy.json, docker build,   │
  │                                            │   docker run, healthcheck            │
  │                                            │                                    │
  │                                            │ ◀ POST .../progress {completed,      │
  │                                            │     result:{deployment_id, ports}}   │
  │                                            │         task.completed               │
  │                                            │         deployment.completed         │
  │                                            │                                    │
  │  GET /v1/deployments/:id                   │                                    │
  │ ─────────────────────────────────────────▶ │                                    │
  │  ◀ {deployment: {status:"running",          │                                    │
  │      health_status:"healthy", ports}}       │                                    │
```

Deployments are created through `POST /v1/deployments`, which wraps task
creation: the agent gets a deployment row to watch and a task row that does
the work. In `mode: "manual"` the task parks in `awaiting_approval` until an
agent with `approve_deployments` calls `POST /v1/tasks/:id/approve`.

## Resumability

The **task row is the source of truth**, not the agent's session:

- If the creating agent vanishes after `POST /v1/tasks`, the worker still
  claims and executes the queued task, stores `result`/`error` on the row,
  and the agent (or a different agent, or a human) reads it later with
  `GET /v1/tasks/:id`.
- If a worker dies mid-task, the task row shows the last reported progress
  and log chunks; another worker can pick the work back up and the retry
  budget (`attempts` / `max_attempts`) is enforced by the control plane.
- **Retry policy.** A worker-reported `failed` becomes `retrying` (then back
  to `queued` via the sweeper) when `attempts < max_attempts` and the task
  type is safe to re-run from where it failed: idempotent reads always;
  `deploy`/`restart`/`start`/`stop`/build tasks only when the attempt never
  reached `running` (failed while still `claimed`); `remove`/`rollback` never
  auto-retry. Retried tasks keep the same idempotency key, so a duplicate
  report replays instead of double-applying.
- **Claim leases.** Every claim records `lease_expires_at` (now +
  `TASK_CLAIM_LEASE_S`, default 600s), refreshed on each progress report —
  an actively-reporting worker is never swept. The stuck-task sweeper
  requeues tasks stranded in `claimed`/`running` past their lease
  (`task.requeued`, attempt counted) or fails them when the retry budget is
  exhausted / the retry policy forbids it (`task.failed`).
- **Zero-downtime same-version redeploy.** Container names are unique per
  deployment attempt (`uaht-<project>-<version>-<deployment[:8]>`), so
  redeploying a version never stop+rms the live container: it keeps serving
  until the new container passes health, then it is stopped (kept, not
  removed) for rollback. The actual name lives in the worker's `state.json`,
  which is what reconcile and the lifecycle handlers use.
- **Port registry.** `POST /v1/deployments` accepts an optional fixed
  `host_port`, reserved in the `port_allocations` table (unique per
  host+port → 409 on collision). The worker verifies the port is free at OS
  level (bind test) and Docker level (`docker ps` published-port scan)
  before `docker run`, failing the task with a clear error on collision.
  Reservations release when the deployment reaches `failed`/`rolled_back`/
  `stopped` or is superseded by a newer deployment of the same
  project+host; `deployments.ports` is written from the deploy task's
  `result.ports` on completion.
- Deployment rows carry `status` through the full lifecycle
  (`requested → building → starting → healthcheck → running`, with
  `failed` / `rolled_back` / `stopped` as terminal branches), so a
  reconnecting dashboard or agent always sees the current truth.
- Events are append-only and cursor-paginated (`?since=`, `?cursor=`), so a
  consumer that dropped the SSE stream can backfill exactly what it missed.

## Host reliability

The worker and control plane defend the "hosts vanish and come back" reality:

- **Post-reboot reconciliation.** On startup the worker compares its local
  deployment registry (`<work_dir>/deployments/*/state.json`) against actual
  Docker state: stopped containers are started, missing containers are
  recreated from their stored spec (image, port mapping, non-secret env —
  secrets are never persisted locally and are not restored by a reboot), and
  a summary rides along on the next heartbeat. A corrupt `state.json` is
  quarantined aside (`state.json.corrupt-<timestamp>`) instead of crashing
  the worker.
- **Reconnect backoff.** Heartbeat and task-claim failures back off
  exponentially (5s → 300s cap, ±25% jitter), driven by the consecutive
  failure counter and reset on success. Waits always go through the shutdown
  event, so SIGTERM stays responsive during backoff.
- **Crash-loop detection.** Each heartbeat observes every running
  deployment's Docker `RestartCount`; when restarts rise by
  `WORKER_CRASH_LOOP_THRESHOLD` (default 5) within
  `WORKER_CRASH_LOOP_WINDOW_S` (default 300s), the container is explicitly
  stopped, flagged locally (`crash_loop`), and reported via the heartbeat
  `issues` array — the control plane emits `service.crash_loop` on first
  appearance. The worker never restarts a flagged container.
- **Stale-host sweeper.** The control plane marks hosts `degraded` /
  `offline` (emitting `host.degraded` / `host.offline`) when heartbeats stop
  arriving (`HEARTBEAT_DEGRADED_AFTER_S` / `HEARTBEAT_OFFLINE_AFTER_S`,
  defaults 90s/300s). Heartbeats revive `offline`/`degraded` hosts to
  `online` but never overwrite an operator-set `draining` state.

## Idempotency

`POST /v1/tasks` and `POST /v1/deployments` accept an `idempotency_key` in
the JSON body (a client-generated UUID, strongly recommended):

- Same key + *equal* payload → the original object is returned with
  HTTP 200 and `"idempotent_replay": true`. No duplicate task is created.
- Same key + *different* payload → HTTP 409 `conflict`. This catches the
  "retry with a mutated body" bug class instead of silently double-deploying.

Retries are therefore safe by construction: the agent retries the exact
same request until it gets a response, and the worst case is a replay of
the already-recorded answer.

## Multi-app hosting & isolation

One worker host runs many applications side by side. Every deployment is
isolated on four axes, and lifecycle operations are scoped to the
deployment's own recorded resources — never by name-prefix matching:

- **Container names** are unique per deployment attempt
  (`uaht-<project>-<version>-<deployment[:8]>`), so redeploying the same
  version never stops the live container before the new one passes health.
  Compose deployments share one project name per project
  (`uaht-<project>`); a redeploy replaces its own stack in place.
- **Directories**: each deployment owns
  `<work_dir>/deployments/<deployment_id>/` (state.json, extracted
  source, compose file). Stopping or removing one deployment never touches
  another's directory.
- **Ports**: every host port is verified free at OS level (bind test) and
  Docker level (`docker ps` published-port scan) before `docker run` — and,
  since Phase 4, before `docker compose up` too (ports are read from
  `docker compose config --format json`; ports already held by the same
  compose project's stack are skipped, since redeploy replaces it). A
  collision fails the task before anything starts. The control plane's
  `port_allocations` registry reserves fixed ports per deployment.
- **Env & logs**: non-secret env is persisted in state.json, secret env is
  injected at run time and never written to disk; logs are per deployment
  (`logs/deployments/<deployment_id>.log`).
- **Resource limits** from the manifest (memory/cpu) are passed to
  `docker run`.

**Compose rollback.** On healthcheck failure the worker tears down the new
unhealthy stack (`compose down`) and restores the previous one
(`compose up` with the compose file recorded in the previous deployment's
state). The `rollback` task handler restores compose targets the same way;
container targets are still restored with `docker start`.

**Garbage collection.** After every successful deploy (never during one)
the worker keeps `DEPLOY_KEEP_GENERATIONS` (default 2) newest generations
per project and collects the rest: stopped containers are `docker rm`'d,
worker-built images are `docker rmi`'d, compose generations get
`compose down`. GC never removes an image still referenced by a kept
generation, never removes a prebuilt/external image, and never touches a
container docker still reports as running. State directories are kept.

## Why no inbound connections to the host

The host worker only ever makes outbound HTTPS calls. There is no open
port to scan, no SSH key for agents to hold, no callback URL the control
plane must reach. Hosts are referenced by `host_id` / `host_name` only —
IP addresses never appear in requests or responses. This is what makes the
system work behind NAT, on laptops, and on machines whose network identity
changes: as long as the worker can reach the control plane, it can do work.
