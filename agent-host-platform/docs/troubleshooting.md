# Troubleshooting — Universal AGT

Symptom → cause → fix, drawn from the failure modes the code actually
handles. Check the cheap things first: `GET /v1/health`, `agent-host
hosts`, `agent-host tasks --status <state>`, and the event journal
(`agent-host events`).

## Tasks

### Task stuck in `claimed` / `running` forever

**Cause:** the worker died (or lost the network) mid-task and never
reported progress. **This is expected and handled:** every claim records
`lease_expires_at` (`TASK_CLAIM_LEASE_S`, default 600s), refreshed on each
progress report. The stuck-task sweeper (every `TASK_SWEEP_INTERVAL_S`,
default 60s) requeues lease-expired tasks (`queued`, attempt counted,
`task.requeued` event) — or fails them (`task.failed`) when the retry
budget (`attempts >= max_attempts`) is exhausted or the retry policy
forbids auto-retry (`remove`/`rollback` never retry).

**Fix:** usually none — wait ~60s past the lease and watch the task return
to `queued` and get claimed again. If it keeps failing, read the task's
`error` and the worker journal on the host; the task is telling you the
real problem.

### Deployment `POST` returns 409 on retry

**Cause:** you re-sent an `idempotency_key` with a *different* body. The
compare basis for deployments is
`project_id`/`host_id`/`version`/`artifact_id`/`mode`/`host_port` (for
tasks: `type`+`payload`).

**Fix:** re-send the *identical* body with the same key → 200 replay with
`idempotent_replay: true`. If you actually changed something, mint a new
key.

### Manual-mode task is never claimed

**Cause:** not a bug — the task is *created* in `awaiting_approval`. No
worker can claim it until an agent with `approve_deployments` approves it.

**Fix:** approve in the dashboard Approvals panel (dossier + confirmation
modal) or `agent-host approve --task <id>`. Reject with
`agent-host reject --task <id>`.

### Task reports `failed` immediately with a policy error

**Cause:** the worker's policy layer rejected the task before executing
anything (e.g. a `docker-run` payload carrying the removed `extra_args`
field → `TaskRejected`).

**Fix:** read the task's `error` — it names the offending field. Fix the
payload and re-submit (new idempotency key).

## Artifacts

### Upload fails `413 payload_too_large`

**Cause:** the artifact exceeds `ARTIFACT_MAX_BYTES` (default 500 MB),
enforced on `Content-Length` upfront, per-chunk while streaming, and on the
declared `size` at init.

**Fix:** shrink the artifact (multi-stage Dockerfile, `.dockerignore`),
or raise `ARTIFACT_MAX_BYTES` on the control plane.

### Worker fails the deploy: checksum mismatch

**Cause:** `artifact_checksum` in the task payload (injected by
`POST /v1/deployments` from the verified artifact row) doesn't match the
downloaded bytes — tampering in transit/at rest, or a corrupted store.

**Fix:** the worker quarantines and fails safely (nothing is built).
Re-upload the artifact and re-deploy. If it recurs, check the artifact
store disk and the `ARTIFACT_DIR` volume.

### Host gets 403 downloading an artifact

**Cause:** download scoping — a host token may only download artifacts
referenced by tasks *that host claimed* (`payload.artifact_id`). Anything
else is 403 by design.

**Fix:** don't hand-roll worker downloads; let the claim/task flow carry
the artifact. Agents with `read_status` keep their download access.

## Hosts

### Host shows `degraded` / `offline`

**Cause:** the stale-host sweeper: no heartbeat for
`HEARTBEAT_DEGRADED_AFTER_S` (default 90s) → `degraded`
(`host.degraded`); `HEARTBEAT_OFFLINE_AFTER_S` (default 300s) → `offline`
(`host.offline`). Heartbeats revive the host to `online` — but never
overwrite an operator-set `draining` state.

**Fix:** on the host: `systemctl is-active agent-host-worker`, then the
journal. Heartbeat/claim failures back off exponentially (5s → 300s cap,
jittered) — a short outage self-heals. If the worker is active but
heartbeats aren't landing, check outbound HTTPS to the control plane
(URL in `/opt/agent-host/config/worker.env`) and the host token.

### Service is `crash_loop` / container keeps restarting

**Cause:** the container's Docker `RestartCount` rose by
`WORKER_CRASH_LOOP_THRESHOLD` (default 5) within
`WORKER_CRASH_LOOP_WINDOW_S` (default 300s). The worker explicitly stops
it, flags it locally (`crash_loop` in its `state.json`), and reports it in
the heartbeat `issues` array (`service.crash_loop` event on first
appearance). The worker will **never** restart a flagged container.

**Fix:** fix the app (read its logs first: `agent-host logs --deployment
<id>`) and **redeploy**. The flag lives in the worker's local state —
there is no API/CLI to clear it; a new deployment is the clean recovery.

### Corrupt `state.json`

**Cause:** e.g. disk issue or a killed write. The worker quarantines it
aside as `state.json.corrupt-<timestamp>` in the deployment's directory
and keeps running — it never crashes the worker or poisons heartbeats.

**Fix:** inspect the quarantined file on the host
(`<work_dir>/deployments/<id>/state.json.corrupt-*`); redeploy if the
deployment's desired state is lost. Reconciliation on the next start
rebuilds from Docker reality where possible.

### After a reboot, a container is missing but the API said `running`

**Cause:** the container was `docker rm`'d (or its image removed) while the
host was down. On every start the worker reconciles its registry against
actual Docker state: stopped containers are started, missing ones
recreated from the stored spec (image, ports, non-secret env — **secrets
are never persisted locally and are not restored by a reboot**; a
redeploy re-injects them).

**Fix:** usually none — reconcile handles it and the summary rides the
next heartbeat. If secrets are missing after a reboot-recreate, redeploy.

## Deployments

### `host_port` collision → 409 at `POST /v1/deployments`

**Cause:** the port registry (`port_allocations`, unique per host+port).
Reservations release when the deployment reaches `failed`/`rolled_back`/
`stopped` or is superseded by a newer deployment of the same project+host.

**Fix:** omit `host_port` (let the worker pick a free port) or choose
another. The worker additionally bind-tests every port at OS and Docker
level before `docker run` — a collision there fails the task pre-run with
a clear error, never half-started.

### Rollback fails

The Phase 3 rollback-500 (unique violation on re-insert) is fixed; rollback
is a `type=rollback` task against the previous healthy deployment of the
same project+host. If a rollback task fails now:

**Check:** the `deployment.rollback_failed` event and the task `error`.
Common causes: no previous healthy deployment exists (nothing to restore),
or the previous generation was already garbage-collected
(`DEPLOY_KEEP_GENERATIONS`, default 2 — older generations' containers are
`docker rm`'d, images `docker rmi`'d, state dirs kept). Rollback tasks never
auto-retry.

### Same-version redeploy had downtime (older note)

Fixed: container names are unique per deployment attempt, so a redeploy
keeps serving the old container until the new one passes health, then
stops (keeps, not removes) the old one for rollback.

## Ingress / domains

### Tunnel routes don't work — hostname resolves but nothing answers

**Cause (most common):** dashboard-created (token) tunnels read public
hostnames from the tunnel's **dashboard config**, not from the worker's
`config.yml`. The worker writes its route table fine, but Cloudflare never
routes the hostname into the tunnel.

**Fix:** Cloudflare dashboard → Zero Trust → Networks → Tunnels →
your tunnel → **Public hostnames** → add each hostname (service
`http://localhost:<port>`; the port is a placeholder — the worker's route
table is authoritative for where traffic actually goes).

### `POST /v1/domains` with `ingress: tunnel` → 422

**Cause:** `TUNNEL_INGRESS_HOSTNAME` is not set on the control plane.

**Fix:** set it to `<tunnel-id>.cfargotunnel.com` (or use `ingress:
direct` with your own ingress + `PUBLIC_INGRESS_HOSTNAME`).

### Worker never starts the tunnel

**Cause:** ingress disabled (`WORKER_INGRESS_ENABLED` false), missing
`WORKER_TUNNEL_TOKEN`, or the `cloudflared` download failed (pinned release
2026.10.0, SHA-256 verified).

**Fix:** check the worker log — it logs clearly and **stays with ingress
disabled**; deployments keep working. Set the token in the host's
`worker.env` and restart (or queue an `ingress-sync` task — the restart
picks up the new token).

## Auth & API

### `401 unauthorized` on register

Missing/wrong `X-Provisioning-Token` while `UAHT_PROVISIONING_TOKEN` is set
on the control plane. Get the operator's token value.

### `403 forbidden` after the first registration (no token configured)

Bootstrap mode: with `UAHT_PROVISIONING_TOKEN` unset, only the *first*
agent registration is open; the rest 403 until the operator sets the
token. (In production the server won't even boot without it.)

### `403 missing permission: X`

Your key lacks the permission. Self-check: `GET /v1/agents/me` returns your
permission set. Permissions are fixed at registration — register a new
agent with the broader set (needs the provisioning token).

### `429 rate_limited`

Over the per-key budget (120/min agent, 600/min host, 10/min/IP
unauthenticated). Back off with jitter and retry — mutating endpoints are
idempotency-keyed, so a retried identical request is safe.

### Dashboard shows 401 / the sign-in gate re-arms

The agent key lives in `sessionStorage` (cleared when the tab closes).
Re-enter it. Note the SSE stream is the *only* endpoint that accepts
`?api_key=` (because `EventSource` can't set headers) — everything else
needs the `Authorization` header.

### CLI: "control plane base URL required"

Set `UAHT_BASE_URL` (not `UAHT_API_URL`) and `UAHT_API_KEY`, or pass
`--base-url` / `--api-key`.

## Worker updates

### Update rolled back / worker still on the old version

The updater health-gates restarts: the new worker must write a boot marker
(`<work_dir>/worker.status.json`: version + boot timestamp) and look
healthy within `WORKER_UPDATE_HEALTH_TIMEOUT_S` (default 120s), else the
`current` symlink rolls back and the previous release restarts.

**Fix:** run `agent-host-worker --self-check` on the host to see what the
new release failed; check the updater log. Manual recovery: restart the
service — the previous release is kept.

## Secrets

### Secrets missing inside the container after deploy

**Cause:** `DATA_ENCRYPTION_KEY` was changed/rotated — it invalidates
previously stored secrets (AES-256-GCM can't decrypt them).

**Fix:** re-set the secrets (`agent-host secrets set --project ...`) and
redeploy. Back up the key; treat rotation as a planned migration.

### Secret appears in logs

The worker's log redaction strips known secret values (≥ 4 chars) from
`log_chunk`s before upload and on disk. If a short value (< 4 chars)
leaks, that's the documented floor — use longer secrets. Secrets never
appear in events, task results, or `state.json`.
