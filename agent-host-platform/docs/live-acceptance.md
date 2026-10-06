# Universal-AGT — Live Acceptance (§§18–19)

**Purpose:** the deterministic suites (host-worker pytest, control-plane
vitest, SDKs, CLI, dashboard static check, and the 12-job CI) all run
against fakes: a subprocess-backed fake Docker client, pg-mem instead of
PostgreSQL, and a stubbed Cloudflare double. This document is the
production smoke-test procedure to run against **real infrastructure**:
a real Ubuntu host with Docker, a real PostgreSQL (Supabase), and a real
Cloudflare account. It also carries the final acceptance checklist
(§19) and the known limitations to read honestly.

**Who runs it:** the operator with the live infrastructure — not the
coordinator. The coordinator has **no Cloudflare API token** and no
persistent host; those are operator prerequisites (see §1).

**Honest scope:** a green smoke test proves the system works on *your*
infrastructure; it does not prove security against an adversary or
replace the local suites. Conversely, the local suites cannot prove
anything that needs a real Docker daemon, real systemd, real SQL
(`FOR UPDATE SKIP LOCKED`, CHECK enforcement), or real Cloudflare —
that is exactly what this procedure covers.

**DNS does not make an outbound-only host reachable.** Creating a DNS
record for a deployment does not, by itself, route public traffic to
it — the host listens on nothing inbound. Public HTTPS requires the
worker-supervised outbound `cloudflared` tunnel (Step 9) with the
hostname registered in the tunnel's **remote** configuration. See
`docs/cloudflare.md`.

---

## 1. Prerequisites (operator-supplied)

- **Supabase project** (or any Postgres 14+) — migrations
  `001_initial.sql` … `010_registration_idempotency.sql` applied in
  order. Apply `001`'s `pgcrypto` extension as superuser once
  (Supabase SQL editor). Use the **direct/session** connection
  (port 5432) for the API — the SSE event bus holds a persistent
  `LISTEN uag_events`, which transaction-mode poolers do not support.
- **Control plane** deployed and reachable over HTTPS
  (`docs/deployment.md` §§12–14).
- **One persistent Ubuntu/Debian host** with Docker installed, where you
  have root (for `install-host.sh` and systemd).
- **Cloudflare account + zone + API token + domain** for the ingress
  steps. The operator creates the token and zone; the coordinator never
  holds them.
- `agent-host` CLI on your workstation, configured with the control
  plane URL and an agent API key (see `UAHT_BASE_URL` / `UAHT_API_KEY`
  below).

## 2. Required environment variables

No secrets are committed anywhere in this repo. Substitute your own
values for every `<redacted>` below. Canonical names first; alias notes
in parentheses.

### 2a. Control plane (API)

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Direct/session connection, e.g. `postgresql://user:<redacted>@db.example.supabase.co:5432/postgres` |
| `DATA_ENCRYPTION_KEY` | yes | 64 hex chars; generated once, kept in the operator's secret manager. **Loss invalidates all stored secrets.** |
| `UAHT_PROVISIONING_TOKEN` | yes | Gates agent registration; the API refuses to boot without it in production |
| `CLOUDFLARE_API_TOKEN` | for DNS mode | Scoped token for DNS ensure/delete in the zone |
| `CLOUDFLARE_ZONE_ID` | for DNS mode | Zone containing the public hostnames |
| `TUNNEL_INGRESS_HOSTNAME` | for tunnel mode | e.g. `<tunnel-id>.cfargotunnel.com` |
| `CLOUDFLARE_TUNNEL_API_TOKEN` | for tunnel mode | Token that manages the tunnel's remote configuration (falls back to `CLOUDFLARE_API_TOKEN`) |
| `CLOUDFLARE_ACCOUNT_ID` | for tunnel mode | Cloudflare account id |
| `PUBLIC_INGRESS_HOSTNAME` | optional | Public hostname for direct mode |
| `PORT` | no | API listen port (default 3000) |
| `ARTIFACT_DIR`, `LOG_DIR`, `DASHBOARD_DIR` | no | Filesystem roots |
| `RATE_LIMIT_AGENT_PER_MIN` / `RATE_LIMIT_HOST_PER_MIN` / `RATE_LIMIT_UNAUTH_PER_MIN` / `UAHT_ROTATE_RATE_PER_MIN` | no | Defaults 120 / 600 / 10 / 10 |
| `HEARTBEAT_SWEEP_INTERVAL_S` / `HEARTBEAT_DEGRADED_AFTER_S` / `HEARTBEAT_OFFLINE_AFTER_S` | no | Defaults 30 / 90 / 300 |
| `PG_POOL_MAX` | no | Default 10 |
| `LOG_LEVEL` | no | `debug` warns in production |
| `ARTIFACT_MAX_BYTES` | no | Upload cap (default 500MB) |
| `TASK_CLAIM_LEASE_S` | no | Claim lease (default 600) |
| `CLOUDFLARE_API_BASE` | no | Override only for testing |

Full contract: `docs/CONFIG.md` and `control-plane/api/.env.example`.

### 2b. Host worker (written to `/opt/agent-host/worker.env`, mode `0600`, by `install-host.sh`)

| Variable | Required | Notes |
|---|---|---|
| `WORKER_CONTROL_PLANE_URL` | yes | Set at install time as `UAHT_CONTROL_PLANE_URL`, e.g. `https://control-plane.example.com` |
| `WORKER_HOST_NAME` | yes | Set at install time as `UAHT_HOST_NAME` |
| `WORKER_HOST_TOKEN` | yes | Provisioned by the installer (`POST /v1/hosts/register` with the provisioning token); stored, never printed |
| `WORKER_HOST_ID` | yes | Provisioned alongside the token |
| `WORKER_TUNNEL_TOKEN` | for tunnel ingress | Set at install time as `UAHT_TUNNEL_TOKEN`; passed to `cloudflared` via environment, never via argv |
| `WORKER_INGRESS_ENABLED` | no | `1` enables tunnel supervision |
| `WORKER_CAPABILITIES` | no | e.g. `docker,docker-compose,ingress` |
| `WORKER_WORK_DIR` / `WORKER_APPS_DIR` | no | Deployment state + app dirs |
| `WORKER_POLL_WAIT` / `WORKER_HEARTBEAT_INTERVAL` | no | Claim long-poll + heartbeat cadence |
| `WORKER_DRAINING` | no | Sticky local drain flag (operator clears via config) |
| `WORKER_CRASH_LOOP_THRESHOLD` / `WORKER_CRASH_LOOP_WINDOW_S` | no | Defaults 5 / 300 |
| `WORKER_LOG_RETENTION_DAYS` | no | Default 30 |
| `WORKER_WORKER_VERSION` | no | Reported in heartbeats |

### 2c. Operator workstation (CLI)

| Variable | Notes |
|---|---|
| `UAHT_BASE_URL` | e.g. `https://control-plane.example.com` |
| `UAHT_API_KEY` | Agent API key, shown once at `POST /v1/agents/register` |

---

## 3. Smoke-test procedure

Run in order. Each step names the goal, the command, and the pass
condition. Times are wall-clock; allow the defaults (healthcheck
timeout 120s, claim lease 600s) unless noted.

### Step 1 — Control plane boots against real Postgres

```bash
cd agent-host-platform/control-plane/api
DATABASE_URL="<redacted>" \
DATA_ENCRYPTION_KEY="<redacted>" \
UAHT_PROVISIONING_TOKEN="<redacted>" \
npm start
curl -sk https://control-plane.example.com/v1/health
```

**Pass:** `{"ok":true,...}`; the log shows migrations `001`–`010`
already applied (or applied on boot); no superuser error. Then verify
against the DB: `select * from schema_migrations;` shows all ten.

### Step 2 — Registration gate + first agent key

```bash
export UAHT_BASE_URL=https://control-plane.example.com
# no provisioning token -> 401
curl -sk -X POST "$UAHT_BASE_URL/v1/agents/register" \
  -H 'Content-Type: application/json' \
  -d '{"name":"smoke-agent","permissions":{"deploy":true,"read_status":true,"approve_deployments":true}}'
# with the token -> 201, key shown ONCE (save it in your secret manager)
curl -sk -X POST "$UAHT_BASE_URL/v1/agents/register" \
  -H 'Content-Type: application/json' \
  -H "X-Provisioning-Token: <redacted>" \
  -d '{"name":"smoke-agent","permissions":{"deploy":true,"read_status":true,"approve_deployments":true}}'
export UAHT_API_KEY="<redacted>"
agent-host projects create --name smoke-app
```

**Pass:** first call 401s; second returns 201 with a `uag_…` key; the
key authenticates `agent-host projects list`.

### Step 3 — Install the worker; prove outbound-only

```bash
# on the Ubuntu host, as root:
curl -fsSL https://<your-mirror>/Universal-AGT/scripts/install-host.sh -o /tmp/uagt-install.sh
sudo UAHT_CONTROL_PLANE_URL="https://control-plane.example.com" \
     UAHT_HOST_NAME="smoke-host-01" \
     bash /tmp/uagt-install.sh
systemctl is-active agent-host-worker
ss -tlnp | grep -c agent-host || true     # expect 0: the worker listens on NOTHING
journalctl -u agent-host-worker --since '5 min ago' | grep -i heartbeat
agent-host hosts list                     # smoke-host-01 online, last_seen fresh
```

**Pass:** service `active`; zero listening sockets owned by the
worker; heartbeat lines in the log; the host shows `online`.

### Step 4 — Full deploy chain on real Docker

```bash
cd agent-host-platform/examples/demo-app
tar -czf /tmp/demo-app.tar.gz .
agent-host deploy --project smoke-app --version 1.0.0 --host smoke-host-01 \
  --artifact /tmp/demo-app.tar.gz
agent-host deployments list --project smoke-app   # -> running
DEP=$(agent-host deployments list --project smoke-app --json | \
  python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
# --- from here down: on the HOST (ssh or console) ---
CONTAINER=$(docker ps -qf name=uaht-smoke-app)
PORT=$(docker port "$CONTAINER" | grep -oP ':\K[0-9]+' | head -1)
curl -s http://127.0.0.1:$PORT/health
docker ps --filter name=uaht-smoke-app --format '{{.Names}} {{.Status}}'   # Up
```

**Pass:** deployment `running`; `/health` → `{"ok": true}`; the
`uaht-smoke-app-…` container is `Up`; `agent-host events list` shows
the chain `deployment.requested task.created task.claimed
task.started deployment.started task.completed deployment.completed`.

(The remaining steps' `curl`/`docker` commands run **on the host**
via ssh or console; `<redacted>`/API calls run from your
workstation.)

### Step 5 — Health-fail auto-rollback on real Docker

```bash
mkdir /tmp/sick-app && cp -r agent-host-platform/examples/demo-app/* /tmp/sick-app/
python3 - <<'EOF'
import json
p = '/tmp/sick-app/agent.deploy.json'
m = json.load(open(p)); m['env']['HEALTH_FAIL'] = '1'; m['env']['APP_VERSION'] = '9.9.9'
json.dump(m, open(p, 'w'))
EOF
tar -czf /tmp/sick-app.tar.gz -C /tmp/sick-app .
agent-host deploy --project smoke-app --version 9.9.9 --host smoke-host-01 \
  --artifact /tmp/sick-app.tar.gz
sleep 150   # default healthcheck timeout 120s + rollback
agent-host deployments list --project smoke-app
# on the host ($PORT from Step 4 still valid — rollback restores the same host port):
docker ps --filter name=uaht-smoke-app --format '{{.Names}}'
curl -s http://127.0.0.1:$PORT/health && curl -s http://127.0.0.1:$PORT/version
```

**Pass:** 9.9.9 ends `failed`/`rolled_back`; its container is gone;
the 1.0.0 container is `Up` again; `/version` reports the OLD
version; `/health` is 200. The restored target's `health_status` is a
real check result, never assumed.

### Step 6 — Explicit rollback + lifecycle ops

```bash
# explicit rollback to the 1.0.0 deployment (idempotent, never auto-retries):
agent-host rollback --deployment <9.9.9-deployment-id>
# lifecycle on the live deployment (on the host where noted):
agent-host restart --deployment "$DEP"
agent-host status --deployment "$DEP"
agent-host logs --deployment "$DEP" | head -20
agent-host stop --deployment "$DEP"
agent-host start --deployment "$DEP"
curl -s http://127.0.0.1:$PORT/health   # on the host
```

**Pass:** explicit rollback completes (`deployment.rolled_back`
event); restart/stop/start cycle returns the app to `running`;
`/health` is 200 again. **Verify-before-destroy:** a rollback
against a bogus target id fails cleanly while the current deployment
keeps serving.

### Step 7 — Environment update + secrets

```bash
# store a secret, then deploy: the worker pulls the project's stored
# secrets itself (host-token-only channel) and injects them as container env
agent-host secrets set --project smoke-app --name API_TOKEN --value '<redacted>'
mkdir /tmp/colored-app && cp -r agent-host-platform/examples/demo-app/* /tmp/colored-app/
python3 - <<'EOF'
import json
p = '/tmp/colored-app/agent.deploy.json'
m = json.load(open(p)); m['env']['APP_COLOR'] = 'blue'
json.dump(m, open(p, 'w'))
EOF
tar -czf /tmp/colored-app.tar.gz -C /tmp/colored-app .
agent-host deploy --project smoke-app --version 1.1.0 --host smoke-host-01 \
  --artifact /tmp/colored-app.tar.gz
sleep 60
docker exec $(docker ps -qf name=uaht-smoke-app) env | grep -E 'API_TOKEN|APP_COLOR'
grep -r '<redacted>' /opt/agent-host/ || echo "secret not on worker disk"

# environment-update on a live single-container deployment (via the API):
DEP11=$(agent-host deployments list --project smoke-app --json | \
  python3 -c "import json,sys; ds=json.load(sys.stdin); print([d['id'] for d in ds if d['version']=='1.1.0'][0])")
curl -sk -X POST "$UAHT_BASE_URL/v1/tasks" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $UAHT_API_KEY" \
  -d "{\"type\":\"environment-update\",\"payload\":{\"deployment_id\":\"$DEP11\",\"env\":{\"APP_COLOR\":\"green\"}},\"idempotency_key\":\"smoke-env-1\"}"
sleep 10
docker exec $(docker ps -qf name=uaht-smoke-app) env | grep APP_COLOR
```

**Pass:** `API_TOKEN` present in the container env; absent from
worker disk, logs (`***` in their place), and task results.
**Known limitation:** `environment-update` on a *compose*
deployment raises `deployment has no container to update`
(compose-file env rewrite is not implemented) — redeploy with new
env instead.

### Step 8 — Compose deployment + compose lifecycle parity

```bash
# build a minimal compose app from the demo-app (fixed host port 18081 —
# pick another free port on your host if it is taken):
mkdir -p /tmp/compose-app
cp agent-host-platform/examples/demo-app/Dockerfile \
   agent-host-platform/examples/demo-app/server.py /tmp/compose-app/
python3 - <<'EOF'
import json
m = json.load(open('agent-host-platform/examples/demo-app/agent.deploy.json'))
m['name'] = 'smoke-compose'
m['runtime'] = 'docker-compose'
json.dump(m, open('/tmp/compose-app/agent.deploy.json', 'w'))
EOF
cat > /tmp/compose-app/docker-compose.yml <<'EOF'
services:
  web:
    build: .
    ports:
      - "18081:3000"
EOF
tar -czf /tmp/compose-app.tar.gz -C /tmp/compose-app .
agent-host projects create --name smoke-compose
agent-host deploy --project smoke-compose --version 1.0.0 --host smoke-host-01 \
  --artifact /tmp/compose-app.tar.gz
DEP_C=$(agent-host deployments list --project smoke-compose --json | \
  python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
agent-host deployments list --project smoke-compose   # -> running
curl -s http://127.0.0.1:18081/health                 # on the host
agent-host restart --deployment "$DEP_C"
agent-host stop --deployment "$DEP_C"
agent-host start --deployment "$DEP_C"
agent-host status --deployment "$DEP_C"
agent-host logs --deployment "$DEP_C" | head -20
```

**Pass:** the compose stack reaches `running` and `/health` is 200;
restart/stop/start/logs/status all resolve the compose project (no
"has no container" errors); the stack is `Up` at the end. This step
exercises the compose parity fixed in the 2026-10-06 follow-up pass.

### Step 9 — Cloudflare tunnel ingress (public HTTPS)

**Prerequisite reminder:** DNS alone cannot reach this host. Public
traffic needs the worker-managed outbound tunnel.

```bash
# on the host, reinstall with ingress (reuses stored credentials, never blanks them):
sudo UAHT_CONTROL_PLANE_URL="https://control-plane.example.com" \
     UAHT_HOST_NAME="smoke-host-01" UAHT_INGRESS_ENABLED=1 \
     UAHT_TUNNEL_TOKEN="<redacted>" \
     bash /tmp/uagt-install.sh
# Zero Trust dashboard -> Networks -> Tunnels -> <tunnel> -> Public hostnames:
# add smoke.example.com (required for dashboard-created token tunnels —
# Cloudflare reads public-hostname routes from the tunnel's remote
# configuration, not from the worker's local config.yml mirror;
# see docs/cloudflare.md "Control authority")
agent-host domains add --deployment "$DEP" --hostname smoke.example.com --ingress tunnel
sleep 10
curl -s -o /dev/null -w '%{http_code}\n' https://smoke.example.com/health
```

**Pass:** `200` from the public hostname; `agent-host domains
list` shows the entry; the worker log shows the ingress-sync route
write. **Accepted limitation (Phase 7):** for dashboard-created
tunnels, public hostnames must be added in the Cloudflare
dashboard — the worker cannot push them.

### Step 10 — Reboot recovery + crash recovery

```bash
sudo reboot
# ... wait for the host ...
systemctl is-active agent-host-worker
docker ps --filter name=uaht --format '{{.Names}} {{.Status}}'   # back Up
agent-host hosts list   # smoke-host-01 online, last_seen fresh
sudo kill -9 $(systemctl show agent-host-worker -p MainPID --value)
sleep 8
systemctl is-active agent-host-worker   # active (restarted by systemd)
agent-host deploy --project smoke-app --version 1.2.0 --host smoke-host-01 \
  --artifact /tmp/demo-app.tar.gz      # worker still claims work
```

**Pass:** previously-`running` containers are `Up` after reboot
(restart policy + worker reconcile of
`<work_dir>/deployments/*/state.json` vs `docker ps`; a missing
compose stack is recreated from the persisted compose file); the
worker revives after `kill -9`; a fresh deploy is claimed and
completes.

### Step 11 — Sweeper behavior (stuck task, silent host)

```bash
# stop the worker mid-deploy to simulate a dead host:
# (deploy, then within ~5s: sudo systemctl stop agent-host-worker)
sleep 700   # past TASK_CLAIM_LEASE_S (600) + sweeper interval
agent-host tasks list --json | python3 -c \
  "import json,sys; print([(t['id'][:8],t['status']) for t in json.load(sys.stdin)][:5])"
agent-host hosts list   # smoke-host-01 offline/degraded while stopped
sudo systemctl start agent-host-worker
```

**Pass:** the stuck task returns to `queued` and is claimed on
restart; the host flaps `online -> offline/degraded -> online`.
`remove`/`rollback` tasks never auto-retry (by design).

---

## 4. Final acceptance checklist (§19)

All smoke steps green + local suites green + 12-job CI green =
accepted. Record the date, the Supabase project, the host
fingerprint, and the Cloudflare zone with the results.

### Control plane
- [ ] Boots against real Postgres; migrations `001`–`010` applied; `001`'s
      pgcrypto installed as superuser once (non-superuser chain run
      validated — see limitation 2 below)
- [ ] Agent registration gated on `UAHT_PROVISIONING_TOKEN`; wrong token
      → 401 (+ `auth.failed` audit event)
- [ ] Host registration via provisioning token (header or Bearer) or
      `deploy`-scoped agent key; registration idempotent with
      `idempotency_key`
- [ ] Task queue: create/poll/cancel; idempotency keys (tasks, deployments,
      domains, registration); 409 on conflict, `idempotent_replay` on replay
- [ ] Atomic claims: `FOR UPDATE SKIP LOCKED` — exactly one host wins
      under real contention
- [ ] Claim leases + sweepers: stuck tasks requeued/failed on lease
      expiry; silent hosts → `degraded`/`offline` at 90s/300s; never
      overwrite operator-set `draining`
- [ ] Retry policy: only safe types retry; `remove`/`rollback` never
      auto-retry
- [ ] Port registry: fixed-port collision → 409 before any container
      starts; rollback port settlement (`settle-rollback-ports`) reconciles
      on both rollback paths
- [ ] Manual approval: tasks park in `awaiting_approval`; approve/reject
      gated on `approve_deployments`
- [ ] Events: 46 types journaled append-only; SSE stream live
- [ ] Secrets: AES-256-GCM at rest; names-only reads; injected as
      container env; absent from disk, logs, and task payloads
- [ ] Rate limits enforced (120/min agent, 600/min host, 10/min
      unauthenticated IP); request correlation ids (`X-Request-Id`)
- [ ] Dashboard: Approvals panel, service actions, SSE live view; every
      dashboard-called endpoint contract-matched (functional browser run
      still outstanding — see limitation 6)

### Host
- [ ] Installer: prerequisites validated, worker provisioned, systemd
      unit installed + enabled, first heartbeat verified; **zero listening
      sockets** (`ss -tlnp` shows none for the worker)
- [ ] Heartbeats every 30s with full §9 payload (`worker_version`,
      `capabilities`, `ingress`, drain state); reconnect backoff
      (5s→300s, jittered) on control-plane outage
- [ ] Reconcile on boot: `state.json` vs `docker ps` — stopped containers
      started, missing containers/compose stacks rebuilt from the stored
      contract, corrupt state quarantined, never fatal
- [ ] systemd `Restart=always` survives `kill -9`; reboot returns
      containers (restart policy) and the worker (reconcile + heartbeat)
- [ ] Draining: worker pauses claims when `WORKER_DRAINING=true` or the
      plane advertises `draining`; server refuses claims from draining
      hosts
- [ ] Crash-loop detection: container flagged `crash_loop` and stopped,
      never restarted by the worker
- [ ] Self-update: SHA-256-verified tarball, atomic symlink swing,
      post-restart health gate with rollback on failure
- [ ] Log retention: 10MiB×3 rotation; retention pruning on the heartbeat
      loop

### Deployment
- [ ] Artifact upload: two-phase init + PUT; 500MB cap enforced; size +
      SHA-256 verified server-side; stale-pending uploads reaped after 24h
- [ ] Deploy pipeline: checksum re-verified pre-docker; manifest validated
      against PROTOCOL §4; `volumes` in `agent.deploy.json` **rejected**
      with a clear error (compose is the persistence path)
- [ ] Resource admission: transactional reservation; fails fast when the
      request doesn't fit; released on every failure path
- [ ] Health checks: HTTP exact-200 on `127.0.0.1:<port>`, no redirect
      following
- [ ] Rollback: automatic on health-fail **and** explicit via
      `agent-host rollback` both run the unified implementation —
      verify-before-teardown, real post-restore health check, honest
      `health_status`, port settlement on both paths
- [ ] Compose: `restart`/`stop`/`start`/`logs`/`status` work on compose
      deployments; GC collects old compose generations; missing compose
      stacks recreated from the persisted compose file on boot
- [ ] Multi-app isolation: per-deployment names/dirs/ports/logs/env;
      stopping one never touches another
- [ ] `environment-update` recreates single-container deployments with
      merged env; secrets scrubbed from logs/state

### Agent resilience
- [ ] Agent disappearance mid-deploy: deployment completes; state and
      result readable later
- [ ] Manual-approval mode: nothing starts before approve; reject ends
      `cancelled` with zero containers
- [ ] Broken artifact: build failure fails the task, keeps logs, never
      touches the healthy container
- [ ] Health-fail deploy: automatic rollback restores the previous
      version serving
- [ ] Stuck worker: lease expiry requeues the task; another claim wins
      it exactly once
- [ ] SDKs (Python/JS) and CLI drive the full flow: register → artifact →
      deploy → status → logs → rollback → approve/reject → domains

### Networking
- [ ] **DNS ≠ reachability**: a DNS record alone routes nothing to the
      outbound-only host (stated, and the smoke test proves the negative
      is never assumed)
- [ ] Cloudflare DNS mode: idempotent proxied CNAME ensure/delete
- [ ] Tunnel mode: worker-supervised outbound `cloudflared` (token via
      env, never argv); hostname → `127.0.0.1:port` routes; remote tunnel
      configuration is the source of truth (local `config.yml` is a
      diagnostic mirror)
- [ ] Domain lifecycle: `requested → configuring → active → failed`,
      `degraded` on unverifiable routes, `removing → removed`; reconciler
      re-verifies against the remote config every 300s
- [ ] SSRF: health checker follows no redirects; domain probe resolves
      A+AAAA, requires every address public (fail closed), and pins the
      validated IP (informational only, never blocks)
- [ ] Public HTTPS end-to-end through a real domain (tunnel mode)

---

## 5. Known limitations (read before signing off)

1. **Compose `environment-update` not implemented.** The handler
   recreates a container; a compose deployment has no container, so it
   raises `HandlerError("deployment has no container to update")`.
   Workaround: redeploy with the new env. (Tracked as C11 in
   `docs/implementation-status.md`.)
2. **Non-superuser migration validation still outstanding.** CI applies
   the chain as the `postgres` superuser. Statement-level analysis says
   only `001`'s `CREATE EXTENSION pgcrypto` needs elevation, but the
   chain has never been *executed* as a non-superuser owner role on a
   real database.
3. **Cloudflare prerequisites are the operator's.** The coordinator does
   **not** hold a Cloudflare API token, zone, or domain — live
   tunnel/DNS/HTTPS validation (Steps 9, Networking checklist) can only
   be performed by the operator on their account.
4. **Real-host proofs outstanding without the operator's host:** Docker
   daemon behavior, systemd install/start/reboot, cgroup resource
   admission, `FOR UPDATE SKIP LOCKED` under real contention, sweeper
   wall-clock timing, `LISTEN` over the direct connection, live
   `cloudflared` with the env-passed token, worker self-update against a
   live systemd unit.
5. **Dashboard UI:** static checks + dashboard↔route contract matching
   run in CI; no functional browser test has ever exercised the UI.
6. **Accepted trust boundaries (by design, documented):** worker updates
   are checksum-pinned, not code-signed (a compromised control plane can
   push worker code); payload-carried secrets live in plaintext in the
   task row (the encrypted store + worker pull is the preferred path);
   the tunnel token is visible in the host's process table to local root;
   the tunnel's remote configuration is authoritative over the worker's
   local mirror; for dashboard-created tunnels, public hostnames must be
   added in the Cloudflare dashboard.
7. **Design boundaries (not gaps):** health checks are HTTP-only;
   permissions are global per agent key (no per-project ACL);
   `awaiting_approval` tasks have no push/notification channel (agents
   poll); log values under 4 chars are not scrubbed.

---

## 6. Recording results

When the smoke test and checklist are complete, record:

- Date and operator name
- Supabase project id + connection shape used (direct/session)
- Host fingerprint (`hostname`, OS, Docker version, systemd unit state)
- Cloudflare zone + tunnel id (ids only — never tokens)
- Which steps passed, which were skipped and why
- Any deviation from the pass conditions above

File the record next to this doc (e.g.
`docs/live-acceptance-YYYY-MM-DD.md`) so later runs can diff against it.
