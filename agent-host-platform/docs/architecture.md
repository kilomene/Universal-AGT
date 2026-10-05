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
- Deployment rows carry `status` through the full lifecycle
  (`requested → building → starting → healthcheck → running`, with
  `failed` / `rolled_back` / `stopped` as terminal branches), so a
  reconnecting dashboard or agent always sees the current truth.
- Events are append-only and cursor-paginated (`?since=`, `?cursor=`), so a
  consumer that dropped the SSE stream can backfill exactly what it missed.

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

## Why no inbound connections to the host

The host worker only ever makes outbound HTTPS calls. There is no open
port to scan, no SSH key for agents to hold, no callback URL the control
plane must reach. Hosts are referenced by `host_id` / `host_name` only —
IP addresses never appear in requests or responses. This is what makes the
system work behind NAT, on laptops, and on machines whose network identity
changes: as long as the worker can reach the control plane, it can do work.
