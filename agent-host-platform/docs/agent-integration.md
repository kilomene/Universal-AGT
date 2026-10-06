# Agent integration reference — Universal AGT wire protocol v1

Machine-readable integration card. Canonical contract:
`../agent-sdk/protocol/PROTOCOL.md`. Base URL: `https://<control-plane-host>/v1`.
All JSON unless noted. Error shape everywhere: `{"error": {"code": "...", "message": "..."}}`.

## 0. Auth

```
Authorization: Bearer <AGENT_API_KEY>
```

Get the key once from `POST /v1/agents/register` (response field `api_key`,
shown once). Store in secret storage; never in code, logs, tickets, or repos.
Host tokens and agent keys are not interchangeable (403 if crossed).

## 0.1 Credential rotation

Keys rotate without downtime — the old credential stops working the moment
the new one is issued:

```
POST /v1/agents/me/rotate          → 200 {"agent": {...}, "api_key": "<new, shown once>"}
```

SDK: JS `await client.rotateKey()` → `{agent, api_key}`;
Python `client.rotate_agent_key()` → same. Emits `agent.key_rotated`.
(Host tokens: `POST /v1/hosts/:id/rotate-token` with the host token —
emits `host.token_rotated`. Agents cannot rotate host tokens.)

## 1. Register an agent

```
POST /v1/agents/register
{"name": "<unique>", "type": "ci", "capabilities": ["docker"],
 "permissions": {"deploy": true, "restart": true}}
→ 201 {"agent": {"id": "<uuid>", "name": ..., "type": ..., "permissions": {...}, ...},
       "api_key": "<show once>"}
→ 409 {"error": {"code": "conflict", ...}} if name already exists
```

- `type` is a free-form label (default `"generic"`); it gates nothing.
- Registration is gated: send header
  `X-Provisioning-Token: <UAHT_PROVISIONING_TOKEN>` (the operator's token —
  get it from whoever runs the control plane). With the env var unset, only
  the very first registration is open (bootstrap mode); afterwards it 403s.
  In production (`NODE_ENV=production`) the server refuses to boot without it.
- `permissions` is an object of booleans. Valid keys (the enforced
  registry — `src/middleware/auth.ts` `PERMISSIONS`):
  `deploy`, `approve_deployments`, `read_status`, `restart`, `stop`,
  `manage_secrets`, `manage_domains`.
  Only these seven names are ever checked — anything else (e.g.
  `read_logs`) grants nothing. The `logs` task type needs `read_status`;
  `remove` is a task type that needs `deploy`, not a permission.
- Permissions are fixed at registration (no update endpoint). Need more
  later → register a new agent or ask a human operator.
- Verify your own row any time: `GET /v1/agents/me → {"agent": {...}}`
  (agent auth only, no extra permission needed).

Two agents, two permission sets — the control plane treats `type` as an
opaque label; all authorization flows from `permissions` only (no
agent-type special-casing exists in the control plane, SDKs, CLI, or worker):

```bash
# muse: full operator — deploy, restart services, approve manual deploys
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' \
  -d '{"name": "muse", "type": "ci",
       "capabilities": ["docker", "compose"],
       "permissions": {"deploy": true, "read_status": true,
                       "restart": true, "stop": true, "approve_deployments": true}}'
# → 201 {"agent": {...}, "api_key": "<muse-key>"}

# instinct: deploy-only builder — cannot restart/stop/approve
curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' \
  -d '{"name": "instinct", "type": "ci", "capabilities": ["docker"],
       "permissions": {"deploy": true, "read_status": true}}'
# → 201 {"agent": {...}, "api_key": "<instinct-key>"}
```

`instinct` calling `POST /v1/services/<id>/restart` gets
`403 {"error": {"code": "forbidden", "message": "missing permission: restart"}}`;
`muse` succeeds. Same code path, same endpoints — only the permission set
differs. `GET /v1/agents/me` returns each agent's own permissions so an
agent can self-check before attempting an action.

## 2. Projects

```
POST /v1/projects
{"name": "url-shortener", "owner": "...", "repository": "...", "runtime": "docker",
 "configuration": {...}}
→ 201 {"project": {"id": "<uuid>", ...}}

GET  /v1/projects                     → {"projects": [...]}
GET  /v1/projects/:id             → {"project": {...}}
PUT  /v1/projects/:id             → 200 {"project": {...}}   # update configuration
→ 422 on invalid configuration
```

## 3. Artifact upload (init + PUT, sha256-verified twice)

```
SHA=$(sha256sum build.tar.gz | cut -d' ' -f1); SIZE=$(stat -c%s build.tar.gz)

POST /v1/artifacts/init
{"project_id": "<uuid>", "filename": "build.tar.gz", "size": <SIZE>,
 "checksum": "sha256:<SHA>", "version": "1.0.0"}
→ 201 {"artifact": {"id": "<uuid>", "status": ...}, "upload_url": "/v1/artifacts/<id>/content"}

PUT <BASE><upload_url>            # Content-Type: application/octet-stream, raw bytes
→ 2xx on success; 422 if size or sha256 mismatch
```

The worker re-verifies sha256 after download; mismatch → task `failed`,
artifact quarantined. Ship an `agent.deploy.json` manifest at the artifact
root (PROTOCOL §4); the worker validates it before any build and fails fast
on invalid manifests. Validate locally with `examples/validate.py`.

```
GET /v1/artifacts?project_id=    → {"artifacts": [...]}
GET /v1/artifacts/:id            → {"artifact": {...}}
```

## 4. Deployments — automatic vs manual, idempotency

```
POST /v1/deployments
{"project_id": "<uuid>", "version": "1.0.0", "artifact_id": "<uuid>",
 "host_id": "<uuid>",            # optional; omit → any capable host claims it
 "mode": "automatic",            # or "manual"
 "host_port": 8080,              # optional fixed host port; requires host_id; 409 on collision
 "idempotency_key": "<uuid>"}
→ 201 {"deployment": {"id": "<uuid>", "status": "requested", ...},
       "task": {"id": "<uuid>", "status": "queued", ...}}
→ 200 {"deployment": ..., "task": ..., "idempotent_replay": true} on key reuse (same body)
→ 409 {"error": {"code": "conflict", ...}} on key reuse with a DIFFERENT body
→ 422 if host_port set without host_id, or artifact not ready / belongs to another project
```

- **Always send `idempotency_key`** (uuid v4). If a request times out,
  re-send the *identical* body with the *same* key → 200 replay, no
  duplicate deployment. Comparison basis: tasks compare `type`+`payload`;
  deployments compare `project_id`/`host_id`/`version`/`artifact_id`/
  `mode`/`host_port`.
- `mode: "automatic"` (default): task flows straight through.
  `mode: "manual"`: the task is *created* in `awaiting_approval` (no worker
  can claim it there) until an agent with `approve_deployments` approves
  (→ `queued`) or rejects (→ `cancelled`).
- Query: `GET /v1/deployments?project_id=&host_id=&status=`;
  detail: `GET /v1/deployments/:id → {"deployment": ..., "task": ...}`.
  Deployment `status`: `requested|approved|building|starting|healthcheck|
  running|failed|rolled_back|stopped|stopping`. Task `result` for a finished
  deploy: `{"deployment_id", "status", "health_status", "ports"}`.

## 5. Tasks — poll to terminal, tail SSE

```
POST /v1/tasks
{"type": "deploy|restart|stop|start|remove|rollback|logs|status|healthcheck|
          build|docker-build|docker-run|docker-compose|environment-update|
          artifact-download|artifact-upload|system-info|ingress-sync",
 "payload": {...}, "idempotency_key": "<uuid>", "priority": 0,
 "host_id": "<uuid>", "mode": "automatic|manual"}
→ 201 {"task": {...}} ; 200 + idempotent_replay on key reuse; 409 on key conflict

GET  /v1/tasks/:id                    → {"task": {...}}
GET  /v1/tasks?status=&type=&host_id=&limit=&cursor=
POST /v1/tasks/:id/cancel            → terminal 'cancelled' (non-terminal only);
                                       needs `deploy` permission or task creatorship
POST /v1/tasks/:id/approve           → needs approve_deployments; awaiting_approval → queued
POST /v1/tasks/:id/reject            → needs approve_deployments; awaiting_approval → cancelled
```

Per-type permission map on `POST /v1/tasks` (exact, from the route code):
`deploy` ← deploy, remove, rollback, build, docker-build, docker-run,
docker-compose, environment-update, artifact-upload, **ingress-sync** ·
`restart` ← restart, start · `stop` ← stop ·
`read_status` ← logs, status, healthcheck, system-info, artifact-download ·
unknown types → `deploy` (defensive: never open).

Payloads by type: `deploy {project_id, version, artifact_id?, manifest?}`;
`rollback {deployment_id, target_deployment_id?}`;
`restart|stop|start|remove|logs|status|healthcheck {deployment_id}`;
`build|docker-build {project_id, artifact_id?, dockerfile?, context?}`;
`docker-run|docker-compose {project_id, compose_file?, service?}`;
`environment-update {deployment_id, env:{...}}`;
`artifact-download|artifact-upload {artifact_id, destination?}`;
`system-info {}`.

Task object: `{"id", "type", "status", "priority", "payload", "result",
"error", "attempts", "max_attempts", "created_by", "assigned_to",
"claimed_by", "created_at", "started_at", "completed_at"}`.

Poll pattern (result persists — you can disconnect and read it days later):

```bash
TASK_ID=...; while :; do
  S=$(curl -s $CP/v1/tasks/$TASK_ID -H "Authorization: Bearer $KEY" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["status"])')
  case $S in completed|failed|cancelled) break;; esac
  sleep 5
done
```

Events (needs `read_status`):

```
GET /v1/events?type=&since=&limit=&cursor=   → {"events": [...], "next_cursor"}
GET /v1/events/stream                        # SSE: "data: {event}\n\n" per event;
                                             # replays last ?limit=N (default 50, max 500),
                                             # then live-pushes; ": ping" keep-alive every 15s
```

Canonical event types — the 49 types the server actually emits
(verified against `control-plane/api/src/routes/` + `lib/` +
`middleware/` 2026-10-06; PROTOCOL §3.8 is the canonical list):

- Agents: `agent.connected`, `agent.key_rotated`, `agent.suspended`
  (operator; key rejected immediately), `agent.resumed` (operator),
  `agent.revoked` (operator; key hash replaced — permanent)
- Auth: `auth.failed` (bad provisioning token at a registration gate)
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
- Hosts: `host.registered`, `host.online`, `host.offline`,
  `host.degraded`, `host.token_rotated`, `host.worker_outdated`
  (heartbeat from a worker below the minimum supported version)
- Secrets: `secret.changed`, `secret.deleted`
- Domains: `domain.requested`, `domain.active`, `domain.degraded`
  (route no longer verifies — reconciler retries), `domain.recovered`
  (degraded route verifies again), `domain.failed`,
  `domain.removed`, `domain.remove_failed`, `domain.reconcile`
  (periodic reconciler pass summary)

Notably absent (they look plausible but are never emitted):
`agent.disconnected`, `deployment.building`, `deployment.healthcheck`,
`healthcheck.passed`/`healthcheck.failed` (worker log strings, not
events), `worker.updated` (worker log string, not an event).
Treat unknown types as opaque: match on the ones you need, ignore the
rest.
Note: `since` on the **stream** is accepted by the SDKs but the server
currently replays by `limit` only — use `GET /v1/events?since=` for
time-filtered history.

SDK polling helpers (no raw HTTP needed):
Python: `client.get_logs(deployment_id=...)`,
`client.deploy(project=..., version=..., wait=True)` →
`(deployment, task)`; JS: `client.getLogs({deploymentId})`,
`client.deploy({project, version, wait: true})` → `{deployment, task}`;
JS `client.streamEvents({since, limit})` async generator;
Python `client.stream_events(since=...)` iterator.

## 6. Logs

Create a `logs` task, poll it to terminal, read `result.logs`:

```
POST /v1/tasks {"type": "logs", "payload": {"deployment_id": "<uuid>"}}
→ 201 {"task": {"id": "<uuid>", "status": "queued"}}
GET /v1/tasks/<id> → {"task": {"status": "completed", "result": {"logs": "..."}}}
```

CLI: `agent-host logs --deployment <id>` (waits, prints text) /
`--follow` (streams chunks). SDK: Python `get_logs(deployment_id=...)`,
JS `getLogs({deploymentId})`; `follow=True` (Python) / `follow: true` (JS)
yields incremental chunks; JS also exposes `client.tailLogs({deploymentId})`
as an async generator of chunks, and Python `client.tail_logs(deployment_id=...)`
as an iterator.

## 7. Services — query, restart, stop, start

```
GET  /v1/services?host_id=&limit=     → running deployments (friendly view;
                                        service id == deployment id)
POST /v1/services/:id/restart        # needs restart
POST /v1/services/:id/stop           # needs stop
POST /v1/services/:id/start          # needs restart
```

Each returns the created task; poll `GET /v1/tasks/:id` to terminal.

## 8. Domains (public hostnames)

Attach public hostnames to a deployment. Needs the `manage_domains`
permission. See [`cloudflare.md`](cloudflare.md) for the three ingress
modes — DNS alone cannot reach an outbound-only host.

```
POST   /v1/domains
{"deployment_id": "<uuid>", "hostname": "api.example.com",
 "ingress": "tunnel"|"direct"}            # optional; default: tunnel when
                                          # TUNNEL_INGRESS_HOSTNAME is set on
                                          # the control plane, else direct
→ 201 {"domain": {"hostname": ..., "ingress": ..., "tunnel_host": ...,
                  "cf_record_id": ..., "status": ...}, "ingress_task_id": ...}
→ 422 if ingress=tunnel but TUNNEL_INGRESS_HOSTNAME is not configured
→ 409 on duplicate hostname for the deployment

GET    /v1/domains?deployment_id=<uuid>   → {"domains": [...]}  (manage_domains)
DELETE /v1/domains
{"deployment_id": "<uuid>", "hostname": "api.example.com"}   # deletes the DNS record too
```

Tunnel mode creates the proxied CNAME `hostname → TUNNEL_INGRESS_HOSTNAME`
and queues an `ingress-sync` task for the host, which rewrites the
worker-managed `cloudflared` route table (`hostname →
http://127.0.0.1:<container-port>`). For dashboard-created tunnels, the
hostname must also be added in **Zero Trust → Networks → Tunnels →
Public hostnames** (Cloudflare reads routes from the dashboard config, not
the worker's `config.yml`).

SDK: JS `client.addDomain(deploymentId, hostname, ingress)` /
Python `client.add_domain(deployment_id, hostname, ingress=...)`
(`ingress`: `'tunnel'` | `'direct'`, omit for the server default);
JS `client.listDomains(deploymentId)` / `client.removeDomain(deploymentId, hostname)`;
Python `client.list_domains(deployment_id)` / `client.remove_domain(deployment_id, hostname)`.
CLI: `agent-host domains add|list|rm --deployment <id> [--hostname ...] [--ingress tunnel|direct]`.

## 9. Rollback

```
POST /v1/deployments/:id/rollback    # needs deploy
→ 201 {"deployment": {...}, "task": {"id": "<uuid>", "type": "rollback", ...}}
```

Creates a `type=rollback` task against the previous healthy deployment of
the same project+host (no new deployment row). The worker's completion
report drives `deployment.rolled_back`; event `deployment.rollback_requested`.
`rollback` tasks never auto-retry.

## 10. Manual deployments — approve / reject

```bash
# as the approver (needs approve_deployments permission):
curl -s -X POST $CP/v1/tasks/<task-id>/approve -H "Authorization: Bearer $APPROVER_KEY"
# → task awaiting_approval → queued (+ deployment status → approved)
curl -s -X POST $CP/v1/tasks/<task-id>/reject -H "Authorization: Bearer $APPROVER_KEY"
# → task awaiting_approval → cancelled
# approving/rejecting a task NOT in awaiting_approval → 409 conflict
```

## 11. Secrets

```
POST   /v1/projects/:id/secrets {"name": "DB_PASS", "value": "..."}  # manage_secrets
GET    /v1/projects/:id/secrets      → [{"name": "DB_PASS", "created_at": "..."}]  # names only
DELETE /v1/projects/:id/secrets/:name
```

Values are encrypted at rest and never returned, logged, or emitted in
events. The worker receives decrypted values only inside the deploy task
payload over its authenticated channel.

## 12. Error codes — what to do

| HTTP | code | Meaning | Agent action |
|------|------|---------|--------------|
| 400 | `bad_request` | malformed body / bad params | Fix the request. Do NOT retry unchanged. |
| 401 | `unauthorized` | missing/invalid Bearer token | Check the key; if revoked, re-register (new identity) or ask a human. Do not retry blindly. |
| 403 | `forbidden` | missing permission (message names it) | Do NOT retry. Self-check via `GET /v1/agents/me`; use a broader-scoped key or ask a human. Permissions are fixed at registration. |
| 404 | `not_found` | bad id / endpoint | Verify the id/URL. Do not retry. |
| 409 | `conflict` | idempotency key reused with *different* body | Resend the *identical* body with the same key (→ 200 replay), or mint a *new* key for the changed body. |
| 409 | `conflict` | duplicate agent name / host port collision | Pick a different name; for ports, omit `host_port` or choose another. |
| 409 | `conflict` | approve/reject on non-`awaiting_approval` task | Read the task status — it already moved on. |
| 422 | `unprocessable` | validation failed (manifest, checksum/size, host_port without host_id, config) | Fix the payload. Do NOT retry unchanged. |
| 429 | `rate_limited` | >120 req/min per agent key | Back off (sleep + jitter), then retry. Do not hammer. |
| 500 | `internal` | control-plane bug/outage | Safe to retry **once** with the **same** idempotency key after a short backoff (replay-safe). If it persists, escalate to a human — do not loop. |

Timeout-with-unknown-outcome on any mutating call → re-send the *identical*
body with the *same* idempotency key. Result is exactly-once: either the
original object (200 replay) or the first creation (201).

## 13. Task state diagram

```
                        ┌─────────────┐
                        │   queued    │◄────────────────┐
                        └──────┬──────┘                 │
         cancel (any non-terminal)│  ┌──────────┐       │ sweeper /
                        │         │ retrying │───────┘ retryable
                        ▼         └────▲─────┘         failure
                 ┌────────────┐        │ (failed, attempts < max)
                 │  claimed   │────────┘
                 └──────┬─────┘
                        ▼
                 ┌────────────┐
                 │  running   │
                 └──────┬─────┘
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
  completed          failed        cancelled
  (terminal)       (terminal)      (terminal)

Manual mode: the task is CREATED in awaiting_approval (before queued):
┌──────────────────┐ approve            ┌─────────┐
│ awaiting_approval ├──────────▶ queued │  …      │
└────────┬─────────┘ reject     └─────────┘
         ▼
     cancelled
(approving/rejecting a non-awaiting_approval task → 409)

Terminal: completed | failed | cancelled.
`failed` may pass through `retrying` (then back to `queued`) when
attempts < max_attempts and the type is safe to re-run (reads always;
deploy/restart/start/stop/build only if the attempt never reached running;
remove/rollback never).
```

## 14. Worked example — deploy url-shortener end to end

Uses `examples/url-shortener` (Node http, zero deps; manifest
`agent.deploy.json` declares port 3000, `/health` check).

```bash
export CP="https://control-plane.example.com"   # no trailing /v1

# 1. register
R=$(curl -s -X POST $CP/v1/agents/register -H 'Content-Type: application/json' \
  -d '{"name": "builder-01", "type": "ci", "capabilities": ["docker"],
       "permissions": {"deploy": true, "read_status": true,
                       "restart": true, "stop": true}}')
export KEY=$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin)["api_key"])')
A() { curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' "$@"; }

# 2. project
P=$(A -X POST $CP/v1/projects -d '{"name": "url-shortener", "runtime": "docker"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["project"]["id"])')

# 3. artifact: tar, sha256, init, PUT bytes
tar -czf /tmp/url-shortener.tar.gz -C examples/url-shortener .
SHA=$(sha256sum /tmp/url-shortener.tar.gz | cut -d' ' -f1)
INIT=$(A -X POST $CP/v1/artifacts/init \
  -d "{\"project_id\": \"$P\", \"filename\": \"url-shortener.tar.gz\",
       \"size\": $(stat -c%s /tmp/url-shortener.tar.gz),
       \"checksum\": \"sha256:$SHA\", \"version\": \"1.0.0\"}")
AID=$(echo "$INIT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["artifact"]["id"])')
UURL=$(echo "$INIT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["upload_url"])')
curl -s -o /dev/null -w "%{http_code}\n" -X PUT "$CP$UURL" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/octet-stream' \
  --data-binary @/tmp/url-shortener.tar.gz     # expect 2xx

# 4. deploy (automatic mode, idempotent)
IK=$(python3 -c 'import uuid; print(uuid.uuid4())')
D=$(A -X POST $CP/v1/deployments \
  -d "{\"project_id\": \"$P\", \"version\": \"1.0.0\", \"artifact_id\": \"$AID\",
       \"mode\": \"automatic\", \"idempotency_key\": \"$IK\"}")
DID=$(echo "$D" | python3 -c 'import sys,json; print(json.load(sys.stdin)["deployment"]["id"])')
TID=$(echo "$D" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["id"])')

# 5. poll task to terminal, then read deployment state
while :; do
  S=$(A $CP/v1/tasks/$TID | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["status"])')
  echo "task: $S"; case $S in completed|failed|cancelled) break;; esac; sleep 5
done
A $CP/v1/deployments/$DID | python3 -m json.tool
# expect deployment.status == "running", health_status == "healthy",
# ports maps the published host port

# 6. logs (task type=logs under the hood)
LT=$(A -X POST $CP/v1/tasks -d "{\"type\": \"logs\", \"payload\": {\"deployment_id\": \"$DID\"}}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["id"])')
while :; do
  LT_S=$(A $CP/v1/tasks/$LT | python3 -c 'import sys,json; t=json.load(sys.stdin)["task"]; print(t["status"])')
  [ "$LT_S" = completed ] || [ "$LT_S" = failed ] || [ "$LT_S" = cancelled ] && break
  sleep 3
done
A $CP/v1/tasks/$LT | python3 -c 'import sys,json; print(json.load(sys.stdin)["task"]["result"]["logs"])'

# 7. operate: restart, then roll back
A -X POST $CP/v1/services/$DID/restart
A -X POST $CP/v1/deployments/$DID/rollback   # against previous healthy version
```

Same flow via CLI (needs `UAHT_BASE_URL`, `UAHT_API_KEY`):

```bash
agent-host agents register --name builder-01 --permissions deploy,read_status,restart,stop
agent-host projects create --name url-shortener --runtime docker
agent-host deploy --project url-shortener --version 1.0.0 --artifact /tmp/url-shortener.tar.gz
agent-host status --deployment <id>
agent-host logs --deployment <id> --follow
agent-host restart --deployment <id>
agent-host rollback --deployment <id>
```

Same flow via SDKs:

```python
from uaht_sdk import UahtClient
# Registration is gated on the provisioning token (X-Provisioning-Token
# header) — no agent key needed yet, so the constructor's api_key is omitted.
bootstrap = UahtClient("https://control-plane.example.com")
reg = bootstrap.register_agent("builder-01", type="ci",
                 permissions={"deploy": True, "read_status": True, "restart": True},
                 provisioning_token="<provisioning-token>")
c = UahtClient("https://control-plane.example.com", reg["api_key"])
p = c.create_project("url-shortener", runtime="docker")["project"]
info = c.init_artifact(p["id"], "/tmp/url-shortener.tar.gz", version="1.0.0")
c.upload_artifact(info["upload_url"], "/tmp/url-shortener.tar.gz")
deployment, task = c.deploy(project="url-shortener", version="1.0.0",
                            artifact_id=info["artifact"]["id"], wait=True)
print(deployment["status"], task["status"])   # running completed
print(c.get_logs(deployment_id=deployment["id"]))
```

```js
import { UahtClient } from "uaht-sdk";
// Registration is gated on the provisioning token (X-Provisioning-Token
// header) — no agent key needed yet, so the constructor's apiKey is omitted.
const bootstrap = new UahtClient({ baseUrl: "https://control-plane.example.com" });
const reg = await bootstrap.registerAgent({ name: "builder-01", type: "ci",
  permissions: { deploy: true, read_status: true, restart: true },
  provisioningToken: "<provisioning-token>" });
const c = new UahtClient({ baseUrl: "https://control-plane.example.com", apiKey: reg.api_key });
const { project } = await c.createProject({ name: "url-shortener", runtime: "docker" });
const info = await c.initArtifact({ project_id: project.id, filePath: "/tmp/url-shortener.tar.gz", version: "1.0.0" });
await c.uploadArtifact(info.upload_url, "/tmp/url-shortener.tar.gz");
const { deployment, task } = await c.deploy({ project: "url-shortener", version: "1.0.0",
  artifactId: info.artifact.id, wait: true });
console.log(deployment.status, task.status); // running completed
console.log(await c.getLogs({ deploymentId: deployment.id }));
```

Note: Python `deploy()` accepts `idempotency_key` on `create_deployment`;
the `deploy()` convenience helper does not mint one — pass your own key
when calling `create_deployment` directly.

## 15. CLI quick map

`agent-host` (env `UAHT_BASE_URL`, `UAHT_API_KEY`; global `--json` before the
subcommand): `agents register|me`, `projects create|list|get|update`,
`deployments list`, `deploy --project --version [--artifact] [--host]
[--mode automatic|manual]`, `status --deployment`, `logs --deployment
[--follow]`, `restart|stop|start --deployment`, `rollback --deployment`,
`approve|reject|cancel --task`, `secrets set|list|delete --project`,
`domains list|add|rm --deployment`, `tasks [--status]`, `events [--follow]`,
`hosts`, `apps [--host]`.
