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

### 2b. Host worker (written to `/opt/agent-host/config/worker.env`, mode `0600`, by `install-host.sh` — `DEFAULT_CONFIG_PATH` in `host-worker/agent/config.py`; override via `$WORKER_CONFIG`)

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

**Pass:** `{"ok":true,...}`; the log shows migrations `001`–`012`
already applied (or applied on boot); no superuser error. Then verify
against the DB: `select * from schema_migrations;` shows all twelve.

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
- [ ] Boots against real Postgres; migrations `001`–`012` applied; `001`'s
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
- [ ] Events: 49 types journaled append-only; SSE stream live
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

---

## 7. Part 6 — Live integration (NOT YET EXECUTED)

> **Status: NOT YET EXECUTED.** None of the live-infrastructure steps
> below has been run. Everything in §§1–6 above is the *code-complete*
> procedure to run; this section is the Part 6 live-integration
> execution plan and verification checklist, to be run once the
> operator's real infrastructure exists. Until it is executed, the
> system is **CODE COMPLETE only** — see §7.7.

### 7.1 Part 6 architecture diagram (live path)

```
                ┌──────────────┐
                │   Internet   │
                └──────┬───────┘
                       │  public HTTPS (tunnel mode only)
                       ▼
                ┌──────────────┐
                │  Cloudflare  │  (zone + tunnel remote configuration)
                └──────┬───────┘
                       │  outbound tunnel connection
                       ▼
 ┌────────────────────────────────────────────────────────────┐
 │  Persistent Ubuntu host (behind NAT/firewall)              │
 │                                                            │
 │   cloudflared (supervised, outbound to Cloudflare)        │
 │        │                                                   │
 │        ▼                                                   │
 │   Host worker (agent-host-worker.service)                  │
 │   ── listens on NOTHING; all traffic is outbound ──        │
 │        │  outbound HTTPS polling: heartbeat / claim /      │
 │        │  progress / artifact download                     │
 │        ▼                                                   │
 │   Docker daemon ──► application containers                 │
 └────────────────────────────────────────────────────────────┘
                       │
                       ▼  outbound HTTPS only
                ┌──────────────┐
                │ Control Plane│  HTTPS API
                └──────┬───────┘
                       │
                       ▼
                ┌──────────────┐
                │ PostgreSQL / │  (Supabase, direct/session connection
                │   Supabase   │   port 5432; migrations 001–012)
                └──────────────┘
```

**Outbound-only fact (verified in code, NOT YET verified live):**
`host-worker/agent/main.py` states the worker is outbound-only and
"never listens on any port"; `host-worker/agent/api.py` states "never
listens on a port; every method below opens an outbound HTTPS"
connection. **The host worker does NOT require inbound SSH from the
control plane and does NOT accept inbound connections from it; it
operates entirely behind NAT/firewalls**, opening only outbound HTTPS
to the control plane and (in tunnel mode) outbound tunnel traffic to
Cloudflare. Migration `001_initial.sql` documents the same rule at the
data layer: "no host IP addresses are stored anywhere — hosts are
reached only through their own outbound connection to the control
plane". The live proof (Step 3's `ss -tlnp | grep -c agent-host`
expecting `0`) is NOT YET EXECUTED — see §7.7.

### 7.2 Part 6A — Supabase / PostgreSQL (NOT YET EXECUTED)

**Exact variable (verified):** `DATABASE_URL` — read by
`control-plane/api/src/db/pool.ts:8` (`process.env.DATABASE_URL`) and
required by `control-plane/api/src/lib/config.ts:64` (boot fails
without it). It is also the connection string for the migration CLI
(`control-plane/api/src/db/migrate-cli.ts:8`).

**Migrations (verified by listing `database/migrations/`):** exactly
twelve files, applied in order —

`001_initial.sql`, `002_artifact_status.sql`,
`003_events_notify.sql`, `004_phase3_reliability.sql`,
`005_events_truncate_block.sql`, `006_token_rotation_grace.sql`,
`007_domains_lifecycle.sql`, `008_task_queue_hardening.sql`,
`009_heartbeat_enrichment.sql`, `010_registration_idempotency.sql`,
`011_project_ownership_acls.sql`, `012_rollback_failed_status.sql`.

No `013` exists. (Re-check `database/migrations/` at execution time
and include any newer migration if one lands before you run.)

**Tables to verify (verified via `CREATE TABLE` in the migration
files):** `agents`, `hosts`, `tasks`, `projects`, `artifacts`,
`deployments`, `events`, `secrets` (all in `001_initial.sql`);
`port_allocations` (added by `004_phase3_reliability.sql`);
`domains` (added by `007_domains_lifecycle.sql`);
`project_members`, `agent_host_access`, `migration_reports` (added by
`011_project_ownership_acls.sql`). Verify live with:

```sql
select count(*) from schema_migrations;            -- expect 12
select table_name from information_schema.tables
 where table_schema = 'public'
   and table_name in ('projects','project_members','agent_host_access',
                      'deployments','tasks','artifacts','domains',
                      'secrets','events','hosts','port_allocations');
```

Only `001`'s `CREATE EXTENSION pgcrypto` needs elevation — apply it
once as superuser (Supabase SQL editor). The remaining eleven
migrations must be executed **as the actual non-superuser production
role**. **Do NOT claim Supabase production readiness until the full
001–012 chain has been executed successfully under that
non-superuser role** — limitation 2 in §5 stands until then.
(Connection shape: direct/session connection, port 5432 — the SSE
event bus holds a persistent `LISTEN uag_events`, unsupported by
transaction-mode poolers.)

### 7.3 Part 6B — Control plane deploy (NOT YET EXECUTED)

Requirements: Node.js, a PostgreSQL reachable via `DATABASE_URL`
(Supabase direct/session), the §2a env vars (`DATABASE_URL`,
`DATA_ENCRYPTION_KEY`, `UAHT_PROVISIONING_TOKEN` — boot refuses
production without the last two), and HTTPS termination.

**Health check (verified in code):** `GET /v1/health` is mounted at
`control-plane/api/src/index.ts:80` with no auth required
(`control-plane/api/src/routes/health.ts:6`). The handler returns
HTTP 200 JSON:

```json
{"ok": true, "version": "<redacted>", "min_worker_version": "<redacted>", "time": "<redacted>"}
```

Verify `ok` is `true` and the version matches the deployed package
before any worker is installed. (Same check as Step 1; the DB row
count check belongs to §7.2.)

### 7.4 Part 6C — Host worker on the persistent Ubuntu host (NOT YET EXECUTED)

**Config variable names (verified against
`host-worker/agent/config.py`, env prefix `WORKER_` stripped from the
`/opt/agent-host/config/worker.env` file keys):**

| Runtime name (worker.env) | Installer input | Notes |
|---|---|---|
| `WORKER_CONTROL_PLANE_URL` | `UAHT_CONTROL_PLANE_URL` | required; `https://…` |
| `WORKER_HOST_NAME` | `UAHT_HOST_NAME` | required |
| `WORKER_HOST_TOKEN` | provisioned by `POST /v1/hosts/register` | required; stored, never printed |
| `WORKER_HOST_ID` | provisioned alongside the token | required |
| `WORKER_TUNNEL_TOKEN` | `UAHT_TUNNEL_TOKEN` | tunnel ingress; passed to `cloudflared` via env, never argv |
| `WORKER_INGRESS_ENABLED` | (`UAHT_INGRESS_ENABLED` install flag) | `1` enables tunnel supervision |
| `WORKER_CAPABILITIES` | — | comma-separated, e.g. `docker,docker-compose,ingress` |
| `WORKER_WORKER_VERSION` | — | reported in heartbeats (`main.py:220`) |
| `WORKER_POLL_WAIT`, `WORKER_HEARTBEAT_INTERVAL` | — | claim long-poll + heartbeat cadence |
| `WORKER_WORK_DIR`, `WORKER_APPS_DIR` | — | deployment state + app dirs |
| `WORKER_DRAINING`, `WORKER_CRASH_LOOP_THRESHOLD`, `WORKER_CRASH_LOOP_WINDOW_S` | — | drain / crash-loop |

**Install via the existing installer/systemd unit — no manual unit
files.** The unit is `agent-host-worker.service`
(`host-worker/agent/agent-host-worker.service`, `SERVICE_NAME="agent-host-worker"`
in `scripts/install-host.sh:101`); the installer copies it to
`/etc/systemd/system/agent-host-worker.service` and runs
`systemctl enable --now agent-host-worker`.

**Heartbeat verification (field names verified in code).** The worker
posts every heartbeat to `POST /v1/hosts/{host_id}/heartbeat`
(`host-worker/agent/api.py:158`); the server
(`control-plane/api/src/routes/worker.ts:167–182`) sets
`status = 'online'` (unless `draining`) and `last_seen = now()`,
and stores `worker_version`, `capabilities`, `total_cpu`,
`total_ram_mb` (the `hosts` row columns, returned by the list query
at `worker.ts:248–251`). Verify live:

```bash
agent-host hosts list    # expect status=online, last_seen fresh
```

and against the DB:

```sql
select name, status, worker_version, capabilities,
       total_cpu, total_ram_mb, last_seen
  from hosts;
```

**Pass:** `status` is `online`, `last_seen` is within the heartbeat
interval, and `worker_version`, `capabilities`, `total_cpu`,
`total_ram_mb` are non-null from the worker's own report. The
worker-side heartbeat payload builder is
`enrich_heartbeat_payload` (`host-worker/agent/main.py:210`), and the
claim long-poll the worker uses is
`POST /v1/worker/tasks/claim?wait=` (`host-worker/agent/api.py:81`).

### 7.5 Part 6D — Real deployment task flow (NOT YET EXECUTED)

Each step below names the code path and the DB state that proves it
happened. Run the flow via Steps 4–5 and query the DB between steps.

1. **Agent POSTs deployment.** `POST /v1/deployments`
   (`control-plane/api/src/routes/deployments.ts`) — validates the
   payload/manifest and the fixed-port request; port collision → 409
   before anything starts.
2. **Validation.** Manifest is validated against PROTOCOL §4;
   `volumes` in `agent.deploy.json` is rejected (compose is the
   persistence path).
3. **Scheduler selects host.** `selectHostForDeployment`
   (`control-plane/api/src/lib/scheduler.ts:99`) picks an online,
   non-draining host with matching capabilities — all inside the
   caller's transaction (`scheduler.ts:99–115`).
4. **Reservation.** Fixed-port reservations land in `port_allocations`
   atomically with the deployment row
   (`deployments.ts:226–290`); resource admission fails fast when the
   request does not fit.
5. **Deployment + task created.** `INSERT INTO deployments …` and
   `INSERT INTO tasks …` with status `queued`
   (`deployments.ts:259`, `deployments.ts:286`).
6. **Worker claims.** Long-poll
   `POST /v1/worker/tasks/claim?wait=` with Bearer `<redacted>` and
   body `{host_id, capabilities}` (`api.py:81–92`); the server claim
   is atomic (`FOR UPDATE SKIP LOCKED`, `worker.ts:297–328`) and
   records the lease (`lease_expires_at = now() +
   TASK_CLAIM_LEASE_S`, default 600s — `taskSweeper.ts:32`).
7. **Artifact download + checksum.** The worker downloads the
   artifact and re-verifies `sha256:<hex>` with a constant-time
   compare before Docker ever runs
   (`host-worker/deployments/pipeline.py:129–136`).
8. **Docker build/run** on the host (Step 4).
9. **Health check.** HTTP exact-200 on `127.0.0.1:<port>`, no
   redirect following; failure triggers the unified automatic
   rollback (Step 5).
10. **Task completed → deployment running.** The worker posts progress
    to `POST /v1/worker/tasks/:id/progress` (`api.py:184`); terminal
    task state and `deployment.completed`/`running` are readable via
    `agent-host deployments list` and the `events` journal.

Verify each step in the DB, not just the CLI output:
`deployments.status`, `tasks.status`, `port_allocations`, and the
`events` rows (`deployment.requested`, `task.created`,
`task.claimed`, `task.started`, `task.completed`,
`deployment.completed`) form the audit trail.

### 7.6 Part 6E — Failure test procedure: kill worker mid-deployment (NOT YET EXECUTED)

Supplements Step 11 with DB-state assertions at every stage:

```bash
# 1. start a deploy (Step 4), then within ~5s on the host:
sudo systemctl stop agent-host-worker        # worker dies mid-deployment
sleep 700                                     # past TASK_CLAIM_LEASE_S (600s) + sweeper interval
```

**Pass conditions — verified against durable DB state, not logs:**

- The task returns to `queued` after lease expiry: the sweeper
  (`control-plane/api/src/lib/taskSweeper.ts`) requeues tasks whose
  `lease_expires_at` has passed with no progress report.
- The host flaps `online → offline/degraded → online`:
  `HEARTBEAT_DEGRADED_AFTER_S` (90s) / `HEARTBEAT_OFFLINE_AFTER_S`
  (300s) sweepers — query `hosts.status`, `hosts.last_seen`.
- Restart the worker; it claims the requeued task and runs the full
  pipeline to the **correct terminal state** — the same worker's boot
  reconcile (`state.json` vs `docker ps`) must not leave a phantom
  deployment behind.
- `remove`/`rollback` tasks never auto-retry (by design); confirm no
  retry rows/events were created for them.
- Final assertion queries: `tasks.status` terminal,
  `deployments.status` matches reality (`docker ps` on the host),
  `events` shows the full chain including the sweep requeue. If the DB
  and Docker disagree, the DB state wins only after the reconcile
  report (`reconciliation` in the heartbeat payload, `main.py:285`)
  is checked — otherwise the test fails.

### 7.7 Part 6F — Cloudflare live test (NOT YET EXECUTED)

Full chain (tunnel mode — the only mode that reaches this host; see
§9 and `docs/cloudflare.md`):

```
Internet ──► Cloudflare (DNS + tunnel remote configuration)
        ──► cloudflared (outbound, worker-supervised; token via env)
        ──► host worker ingress routes ──► Docker container ──► /health
```

**The operator** performs this from an **external network** (not the
host's own LAN), against a real domain
(`app.example.com` in the operator's Cloudflare zone):

1. DNS resolves `app.example.com` to Cloudflare.
2. TLS terminates at Cloudflare (valid public certificate).
3. Cloudflare routes through the tunnel — confirmed by the tunnel's
   **remote** configuration (the local `config.yml` is a diagnostic
   mirror only; `host-worker/ingress/__init__.py:13`).
4. The tunnel reaches the worker's `cloudflared` process
   (`host-worker/ingress/cloudflared.py`: supervised subprocess, token
   via env, never argv, never logged).
5. The worker's ingress routes the hostname to `127.0.0.1:<port>`.
6. The container's `/health` returns HTTP 200 end-to-end:
   `curl -s -o /dev/null -w '%{http_code}\n' https://app.example.com/health`
   from the external network → `200`.

**Do NOT claim public HTTPS until actually tested end-to-end from an
external network.** (Prerequisite: the coordinator holds no Cloudflare
API token, zone, or domain — §5 limitation 3.)

### 7.8 CODE COMPLETE vs LIVE INFRASTRUCTURE VERIFIED

| Area | Code complete | Live infrastructure verified |
|---|---|---|
| Migrations 001–012 chain | yes (local pg-mem / superuser CI) | **NOT YET** — needs real Postgres + non-superuser production role (§7.2) |
| Control plane boot + `GET /v1/health` | yes (tests + contract checks) | **NOT YET** (§7.3) |
| Worker install (systemd unit `agent-host-worker`) | yes (installer + unit file) | **NOT YET** (§7.4) |
| Outbound-only host (zero listening sockets) | yes (code: `main.py`, `api.py`) | **NOT YET** (§7.1, Step 3) |
| Heartbeat fields (`status`, `last_seen`, `worker_version`, `capabilities`, `total_cpu`, `total_ram_mb`) | yes (code + CI) | **NOT YET** (§7.4) |
| Deploy pipeline (validation → scheduler → reservation → claim → checksum → Docker → health → completed) | yes (local + CI) | **NOT YET** (§7.5, Steps 4–5) |
| Kill-worker failure test (lease expiry → requeue → reclaim → terminal state) | yes (logic + sweepers) | **NOT YET** (§7.6, Step 11) |
| Cloudflare tunnel → public HTTPS from external network | yes (code + stubbed double) | **NOT YET** (§7.7, Step 9) |
| Real Docker daemon, real systemd, real SQL (`FOR UPDATE SKIP LOCKED`, CHECK enforcement), real `cloudflared` | n/a (fakes only) | **NOT YET** (§5 limitation 4) |

**Until every row in the right column is executed and recorded per §6,
this document describes a code-complete system awaiting live
acceptance — nothing in §7 has been run, and no live claim is made.**

<!-- END of Part 6 live-integration section -->
