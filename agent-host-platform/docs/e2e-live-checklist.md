# Universal-AGT — Live-Infrastructure E2E Checklist

**Purpose:** the deterministic E2E suite (`host-worker/tests/test_e2e.py` +
`control-plane/api/test/e2eFlows.test.ts`) proves the full chain against a
fake control plane and subprocess-backed "containers". These checks prove the
same chain against **real infrastructure**: a live Supabase/Postgres, a real
Linux host with a real Docker daemon, and (for the ingress check) a real
Cloudflare account. Nothing here is redundant with the local suite — every
item below exercises something the fakes cannot: the real SQL (including
`FOR UPDATE SKIP LOCKED` atomic claims), real container isolation, real
reboots, real network paths.

**Prerequisites**
- A Supabase project (or any Postgres 14+) with the migrations in
  `agent-host-platform/database/migrations/` applied in order
  (`001_initial.sql` … `012_rollback_failed_status.sql`; only `001`'s
  `pgcrypto` extension needs an elevated privilege on a fresh database —
  allowlisted on Supabase, enable via Dashboard → Database → Extensions
  or the SQL editor. Migrations `002`–`012` need only table ownership,
  no superuser. Prefer the direct connection (port 5432) or a
  session-mode pooler for the API — the SSE event bus holds a persistent
  `LISTEN uag_events`, which transaction-mode poolers do not support.)
- The control-plane API running with `DATABASE_URL` pointing at it,
  `DATA_ENCRYPTION_KEY` set (64 hex chars), and `UAHT_PROVISIONING_TOKEN`
  set (gates agent registration).
- One Debian/Ubuntu Linux host with Docker installed.
- The `agent-host` CLI installed locally, with `UAHT_BASE_URL` and
  `UAHT_API_KEY` exported.
- For check 12: a Cloudflare account with a Zone + API token
  (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`), and `TUNNEL_INGRESS_HOSTNAME` set on the
  control plane.

**How to read each check:** *Goal* — what it proves. *Run* — exact commands.
*Pass* — what the output must look like.

---

## 1. Control plane boots against real Postgres

**Goal:** migrations apply cleanly; API starts; health endpoint answers.

```bash
# from agent-host-platform/control-plane/api
for f in ../../database/migrations/*.sql; do
  psql "$DATABASE_URL" -f "$f"
done
DATABASE_URL="$DATABASE_URL" DATA_ENCRYPTION_KEY="<64-hex>" \
  UAHT_PROVISIONING_TOKEN="<pick-a-value>" npm start &
curl -s localhost:3000/v1/health
```

**Pass:** `{"ok":true,...}`; no migration errors (re-running is idempotent).

## 2. Agent registration gate + host provisioning

**Goal:** criterion 1–3 (agents/hosts authenticate; registration is gated).

```bash
export UAHT_BASE_URL=http://<control-plane>:3000
# without the provisioning token -> 401
curl -s -X POST "$UAHT_BASE_URL/v1/agents/register" -H 'Content-Type: application/json' \
  -d '{"name":"e2e-agent","permissions":{"deploy":true}}'
# with the token -> 201, key shown ONCE (save it)
curl -s -X POST "$UAHT_BASE_URL/v1/agents/register" -H 'Content-Type: application/json' \
  -H "X-Provisioning-Token: $UAHT_PROVISIONING_TOKEN" \
  -d '{"name":"e2e-agent","permissions":{"deploy":true,"approve_deployments":true,"read_status":true}}'
export UAHT_API_KEY='<the-shown-key>'
agent-host projects create --name demo-app
```

**Pass:** first call 401s; second returns 201 with a `uag_...` key; the key
authenticates `agent-host projects list`.

## 3. Install the worker on the real host

**Goal:** criterion 5 (outbound-only connection, no inbound ports).

```bash
# on the Linux host, as root:
sudo UAHT_CONTROL_PLANE_URL="http://<control-plane>:3000" \
     UAHT_HOST_NAME=e2e-host-01 \
     ./scripts/install-host.sh
systemctl is-active agent-host-worker
ss -tlnp | grep -c agent-host   # expect 0: the worker listens on NOTHING
journalctl -u agent-host-worker --since '5 min ago' | grep -i heartbeat
```

**Pass:** service `active`; zero listening sockets owned by the worker;
heartbeat lines in the log; `agent-host hosts list` shows the host `online`.

## 4. Full deploy chain with real Docker (criteria 11, 12, 13, 15, 17)

**Goal:** artifact upload → deploy → real `docker build` → real container →
real healthcheck → serving traffic.

```bash
cd agent-host-platform/examples/demo-app
tar -czf /tmp/demo-app.tar.gz .
agent-host deploy --project demo-app --version 1.0.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz
# watch it:
agent-host tasks list --status running
agent-host deployments list --project demo-app
PORT=$(agent-host deployments list --project demo-app --json | python3 -c \
  "import json,sys; print(list(json.load(sys.stdin)[0]['ports'])[0])")
curl -s http://127.0.0.1:$PORT/health   # from the host
curl -s http://127.0.0.1:$PORT/ | head -c 120
docker ps --filter name=uaht-demo-app-1.0.0 --format '{{.Names}} {{.Status}}'
```

**Pass:** deployment reaches `running`; `/health` → `{"ok": true}`;
`/` contains "demo-app"; a `uaht-demo-app-1.0.0-*` container is `Up`;
`agent-host events list` shows, in order,
`deployment.requested task.created task.claimed task.started
deployment.started task.completed deployment.completed`.

## 5. Manual approve / reject (criterion 14)

**Goal:** `awaiting_approval` gates the worker; approve proceeds; reject never
starts a container.

```bash
agent-host deploy --project demo-app --version 1.1.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz --mode manual
agent-host tasks list --status awaiting_approval   # the task sits here
docker ps | grep -c 1.1.0 || true                  # expect 0: nothing started
TASK=$(agent-host tasks list --status awaiting_approval --json | python3 -c \
  "import json,sys; print(json.load(sys.stdin)[0]['id'])")
agent-host approve --task "$TASK"
agent-host deployments list --project demo-app    # 1.1.0 -> running
# second one, rejected:
agent-host deploy --project demo-app --version 1.2.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz --mode manual
TASK2=... # as above
agent-host reject --task "$TASK2"
docker ps | grep -c 1.2.0 || true                  # expect 0, forever
```

**Pass:** nothing for 1.1.0 runs before approve; after approve it reaches
`running`; 1.2.0 never produces a container; task ends `cancelled`.

## 6. Agent disappearance, live (criterion 24)

**Goal:** the deployment completes with no agent polling; state is
retrievable afterwards.

```bash
# terminal A: start a deploy, then kill your own polling (Ctrl-C the watch):
agent-host deploy --project demo-app --version 1.3.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz &
sleep 2; kill %1   # agent "disappears" mid-deploy; nothing else polls
# terminal B (later, even from another machine):
agent-host deployments list --project demo-app    # 1.3.0 -> running
agent-host tasks list --json | python3 -c \
  "import json,sys; ts=json.load(sys.stdin); print([t['status'] for t in ts if t['payload'].get('version')=='1.3.0'])"
```

**Pass:** 1.3.0 reaches `running` with zero agent interaction after the
initial POST; task result contains `ports`; events are complete.

## 7. Broken app keeps the previous version serving (criterion 18)

**Goal:** a failing `docker build` fails the task, keeps logs, and never
touches the healthy container.

```bash
mkdir /tmp/broken-app && cp -r agent-host-platform/examples/demo-app/* /tmp/broken-app/
printf 'nope: {\n' > /tmp/broken-app/Dockerfile   # invalid Dockerfile
tar -czf /tmp/broken-app.tar.gz -C /tmp/broken-app .
agent-host deploy --project demo-app --version 2.0.0-broken --host e2e-host-01 \
  --artifact /tmp/broken-app.tar.gz
agent-host tasks list --json | python3 -c \
  "import json,sys; print([t['status'] for t in json.load(sys.stdin)][:3])"
DEP_BRK=$(agent-host deployments list --project demo-app --json | python3 -c \
  "import json,sys; ds=json.load(sys.stdin); print([d['id'] for d in ds if d['version']=='2.0.0-broken'][0])")
agent-host logs --deployment "$DEP_BRK" | grep -i "build" | head -5
curl -s http://127.0.0.1:$PORT/health   # the 1.3.0 port: still {"ok": true}
docker ps --filter name=uaht-demo-app --format '{{.Names}}'  # 2.0.0-broken absent
```

**Pass:** task `failed`; the deployment's log (`agent-host logs --deployment <dep-id>`)
contains the docker build error; the previous version still serves `/health`;
no container for the broken version exists.

## 8. Health-fail rollback with real Docker (criterion 19)

**Goal:** v2 starts but fails healthchecks → automatic rollback → v1 serving
again, v2 removed.

```bash
mkdir /tmp/sick-app && cp -r agent-host-platform/examples/demo-app/* /tmp/sick-app/
python3 - <<'EOF'
import json
p = '/tmp/sick-app/agent.deploy.json'
m = json.load(open(p)); m['env']['HEALTH_FAIL'] = '1'; m['env']['APP_VERSION'] = '9.9.9'
json.dump(m, open(p, 'w'))
EOF
tar -czf /tmp/sick-app.tar.gz -C /tmp/sick-app .
agent-host deploy --project demo-app --version 9.9.9 --host e2e-host-01 \
  --artifact /tmp/sick-app.tar.gz
sleep 150   # default healthcheck timeout 120s + rollback
agent-host deployments list --project demo-app
docker ps --filter name=uaht-demo-app --format '{{.Names}}'
curl -s http://127.0.0.1:$PORT/health && curl -s http://127.0.0.1:$PORT/version
```

**Pass:** the 9.9.9 deployment ends `failed`/`rolled_back`; its container is
gone; the previous version's container is `Up` again and `/version`
reports the OLD version; `/health` is 200.

## 9. Multi-app + port collision with real Docker (criterion 16)

**Goal:** three apps coexist; stopping one leaves the others; a port
collision fails fast before `docker run`.

```bash
for a in app-a app-b app-c; do
  agent-host projects create --name $a
  mkdir -p /tmp/$a && cp -r agent-host-platform/examples/demo-app/* /tmp/$a/
  python3 - "$a" <<'EOF'
import json, sys
p = f'/tmp/{sys.argv[1]}/agent.deploy.json'
m = json.load(open(p)); m['name'] = sys.argv[1]; m['env']['APP_NAME'] = sys.argv[1]
json.dump(m, open(p, 'w'))
EOF
  tar -czf /tmp/$a.tar.gz -C /tmp/$a .
  agent-host deploy --project $a --version 1.0.0 --host e2e-host-01 --artifact /tmp/$a.tar.gz
done
docker ps --filter name=uaht-app --format '{{.Names}} {{.Ports}}'  # 3 distinct ports
DEP_B=$(agent-host deployments list --project app-b --json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
agent-host stop --deployment "$DEP_B"
docker ps --filter name=uaht-app --format '{{.Names}}'  # app-a, app-c only
# collision: request app-c's port for a new app-a deployment -> must fail pre-run
PORT_C=... # app-c's host port from `agent-host deployments list --project app-c`
curl -s -X POST "$UAHT_BASE_URL/v1/deployments" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $UAHT_API_KEY" \
  -d "{\"project_id\":\"<app-a-uuid>\",\"host_id\":\"<host-uuid>\",\"version\":\"2.0.0\",\"artifact_id\":\"<artifact-uuid>\",\"host_port\":$PORT_C}"
```

**Pass:** three `Up` containers on three distinct host ports; after stopping
app-b, app-a/app-c still answer `/health`; the colliding deploy fails with
a clear "already in use / already published" error and starts no container.

## 10. Reboot recovery (criterion 6)

**Goal:** after a host reboot, containers return (restart policy) and the
worker resumes heartbeats/claims.

```bash
sudo reboot
# ... wait for the host ...
systemctl is-active agent-host-worker
docker ps --filter name=uaht --format '{{.Names}} {{.Status}}'  # back Up
agent-host hosts list   # e2e-host-01 online, last_seen fresh
agent-host deploy --project demo-app --version 1.4.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz   # worker still claims work
```

**Pass:** worker `active`; previously-`running` containers are `Up`
(without manual `docker start`); the host is `online`; a new deploy works.
**What this additionally proves locally (worker E2E covers the logic):**
on every start the worker reconciles its local deployment registry
(`<work_dir>/deployments/*/state.json`) against actual Docker state —
stopped containers are started, missing containers are recreated from their
stored spec, and a corrupt `state.json` is quarantined aside
(`state.json.corrupt-<timestamp>`) instead of crashing the worker. This
check proves the real thing: Docker's restart policy plus the worker's
reconcile on a real reboot.

## 11. Worker crash recovery (criterion 7)

**Goal:** `systemd Restart=always` revives a killed worker.

```bash
sudo kill -9 $(systemctl show agent-host-worker -p MainPID --value)
sleep 8
systemctl is-active agent-host-worker   # active (restarted)
journalctl -u agent-host-worker --since '2 min ago' | grep -ci error || true
agent-host hosts list   # last_seen fresh again
```

**Pass:** service `active` within seconds; no error storm; heartbeats resume.

## 12. Cloudflare tunnel ingress (criteria 33, 34, 35)

**Goal:** a hostname routes to the app through the worker-managed tunnel.

```bash
# control plane: TUNNEL_INGRESS_HOSTNAME=<tunnel-id>.cfargotunnel.com set
# host: reinstall with ingress enabled (Phase 7):
sudo UAHT_CONTROL_PLANE_URL="http://<control-plane>:3000" \
     UAHT_HOST_NAME=e2e-host-01 UAHT_INGRESS_ENABLED=1 \
     UAHT_TUNNEL_TOKEN="<redacted>" \
     ./scripts/install-host.sh
# Zero Trust dashboard -> Networks -> Tunnels -> <tunnel> -> Public hostnames:
# add demo.example.com (required for dashboard-created token tunnels —
# Cloudflare reads public-hostname routes from the tunnel's remote
# configuration, not from the worker's local config.yml mirror;
# see docs/cloudflare.md "Control authority")
DEP=$(agent-host deployments list --project demo-app --json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
agent-host domains add --deployment "$DEP" --hostname demo.example.com --ingress tunnel
sleep 10
curl -s -o /dev/null -w '%{http_code}\n' https://demo.example.com/health
```

**Pass:** `200` from the public hostname; `agent-host domains list`
shows the entry; the worker log shows the ingress-sync route write.
**Accepted limitation (documented in Phase 7):** for dashboard-created
tunnels, public hostnames must be added in the Cloudflare dashboard —
the worker cannot push them.

## 13. Secrets end-to-end (criterion 22)

**Goal:** encrypted at rest, decrypted only for the deploying host, injected
as container env, never in logs/state.

```bash
agent-host secrets set --project demo-app --name API_TOKEN --value 'live-secret-123'
agent-host deploy --project demo-app --version 1.5.0 --host e2e-host-01 \
  --artifact /tmp/demo-app.tar.gz
docker exec <container> env | grep API_TOKEN        # present in container
grep -r live-secret-123 /opt/agent-host/ /srv/agent-apps/ || echo "not on disk"
DEP_S=$(agent-host deployments list --project demo-app --json | python3 -c \
  "import json,sys; ds=json.load(sys.stdin); print([d['id'] for d in ds if d['version']=='1.5.0'][0])")
agent-host logs --deployment "$DEP_S" | grep -c live-secret-123 || true  # expect 0 (scrubbed)
```

**Pass:** secret in container env; absent from worker disk, logs, and task
results (`***` in their place).

## 14. Stuck-task / dead-host sweepers (criteria 25, 26)

**Goal:** a task stuck in `claimed` with no progress is requeued; a silent
host goes `degraded`/`offline`.

```bash
# simulate a dead worker mid-task: claim via API is host-driven, so instead
# stop the worker right after it claims:
# (deploy, then: sudo systemctl stop agent-host-worker within ~5s)
sleep 700  # past TASK_CLAIM_LEASE_S (default 600) + sweeper interval
agent-host tasks list --json | python3 -c \
  "import json,sys; print([(t['id'][:8],t['status']) for t in json.load(sys.stdin)][:5])"
agent-host hosts list   # e2e-host-01 -> offline/degraded while stopped
sudo systemctl start agent-host-worker
```

**Pass:** the stuck task returns to `queued` (event `task.retrying` or
requeue) and is claimed on restart; the host flaps
`online -> offline/degraded -> online` in the hosts list.

---

## Sign-off

All 14 checks green + the local suites (`host-worker` pytest,
control-plane `vitest`, both SDKs, CLI smoke, dashboard static check) green
= the acceptance criteria that can be proven are proven. Record the date,
the Supabase project, and the host fingerprint with the results.
