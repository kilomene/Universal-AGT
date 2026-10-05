# Installing the host worker

The worker is a small daemon that runs on the persistent host (a Linux
machine or VM that stays up). It needs **outbound HTTPS** to the control
plane and **Docker** to run workloads. It never opens inbound ports and
never requires SSH access from anyone.

Prerequisites: a Linux host (x86_64 or arm64) with Docker installed and
running, plus a host token — register the host once against the control
plane (see below) and keep the token somewhere safe on that host only.

> The installer is maintained with the repo at
> `agent-host-platform/scripts/install-host.sh`. Adjust the
> control-plane URL to your deployment.

## One-command install

Run on the host, as root (or with sudo). The script installs the worker,
writes a systemd unit, and starts the service. It takes the control-plane
URL and the host name as environment variables — the host token is either
provisioned automatically at install time or supplied pre-provisioned, and
is written only to the host's own config file, **never into the repo**.

```bash
curl -fsSL https://example.com/Universal-AGT/scripts/install-host.sh -o /tmp/uagt-install.sh
sudo UAHT_CONTROL_PLANE_URL="https://control-plane.example.com" \
     UAHT_HOST_NAME="persistent-host-01" \
     bash /tmp/uagt-install.sh
```

What the installer does:

1. Verifies Docker is present (installs `docker.io` via apt if missing).
2. Creates the `agenthost` system user and lays out `/opt/agent-host`
   and `/srv/agent-apps`.
3. Provisions the host at the control plane (`POST /v1/hosts/register`)
   unless `UAHT_HOST_TOKEN`/`UAHT_HOST_ID` are supplied, and writes
   `/opt/agent-host/config/worker.env` (mode `0600`, agenthost-owned).
4. Installs the systemd unit `agent-host-worker.service` and enables it.
5. Starts the service and waits for the first successful heartbeat.

## Getting a host token (one-time)

From any machine that can reach the control plane. Host registration needs
either the provisioning bearer (`PROVISIONING_TOKEN`, hands-off first
boot) or an agent API key with the `deploy` permission:

```bash
curl -s -X POST https://control-plane.example.com/v1/hosts/register \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $UAHT_PROVISIONING_TOKEN" \
  -d '{"name": "persistent-host-01", "host_type": "linux",
       "capabilities": ["docker", "docker-compose"],
       "worker_version": "1.0.0"}'
# (or: -H "Authorization: Bearer $UAHT_API_KEY" with an agent key that has `deploy`)
# → 201 {"host": {...}, "host_token": "<show once>"}
```

Save the token into the host's environment file, not into chat logs or
tickets. The worker reads `/opt/agent-host/config/worker.env` by default
(override with `WORKER_CONFIG`); the installer writes it there (mode
`0600`, agenthost-owned).

```bash
sudo install -m 0600 -o agenthost -g agenthost /dev/null /opt/agent-host/config/worker.env
# append the WORKER_* keys shown in host-worker/.env.example, e.g.:
sudo tee -a /opt/agent-host/config/worker.env >/dev/null <<'EOF'
WORKER_CONTROL_PLANE_URL="https://control-plane.example.com"
WORKER_HOST_ID="<host uuid from registration>"
WORKER_HOST_TOKEN="<paste token here>"
WORKER_HOST_NAME="persistent-host-01"
EOF
```

## Manual steps (without the installer)

Mirror what the installer does (see `scripts/install-host.sh`): lay out
`/opt/agent-host` (worker tree) and `/srv/agent-apps`, create the
`agenthost` system user, then install the shipped unit
`host-worker/agent/agent-host-worker.service`:

```bash
# 1. Copy the worker (run from the repo root)
sudo mkdir -p /opt/agent-host/worker /srv/agent-apps
sudo cp -r agent-host-platform/host-worker/* /opt/agent-host/worker/
sudo useradd -r -s /usr/sbin/nologin agenthost 2>/dev/null || true
sudo chown -R agenthost:agenthost /opt/agent-host /srv/agent-apps

# 2. Environment file with secrets (see the previous section)
sudo mkdir -p /opt/agent-host/config
sudo install -m 0600 -o agenthost -g agenthost /dev/null /opt/agent-host/config/worker.env
# ... append WORKER_CONTROL_PLANE_URL / WORKER_HOST_ID / WORKER_HOST_TOKEN / WORKER_HOST_NAME ...

# 3. systemd unit (shipped as host-worker/agent/agent-host-worker.service)
sudo cp agent-host-platform/host-worker/agent/agent-host-worker.service \
  /etc/systemd/system/
# Key lines: User=agenthost, WorkingDirectory=/opt/agent-host/worker
# (so `python3 -m agent.main` resolves), ExecStart=
# /usr/bin/python3 -m agent.main --config /opt/agent-host/config/worker.env

# 4. Enable + start
sudo systemctl daemon-reload
sudo systemctl enable --now agent-host-worker.service
```

The shipped unit runs the worker as the `agenthost` user with
`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `MemoryMax=1G`,
and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` (Docker socket +
outbound HTTPS only). The worker reaches the Docker socket through
`ReadWritePaths` — make sure the `agenthost` user can use Docker on your
host (the installer sets this up).

## Verification

On the host:

```bash
sudo systemctl status agent-host-worker.service --no-pager
sudo journalctl -u agent-host-worker.service -n 50 --no-pager   # look for "heartbeat ok"
```

From the control plane side (agent API key):

```bash
curl -s https://control-plane.example.com/v1/hosts \
  -H "Authorization: Bearer $UAGT_API_KEY" | python3 -m json.tool
# expect persistent-host-01 with recent last_seen and cpu/ram/disk stats

# End-to-end: ask the worker to report in
curl -s -X POST https://control-plane.example.com/v1/tasks \
  -H "Authorization: Bearer $UAGT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"type": "system-info", "payload": {}, "host_id": "<host uuid>"}'
# then: GET /v1/tasks/:id → result {cpu, ram, disk, docker, ...}
```

The dashboard (served by the control plane at `/`) shows the host's
status, resource bars, worker version and last-seen time in its Hosts
panel.

## Boot behavior

On every (re)start the worker reconciles its local deployment registry
against actual Docker state **before** it starts claiming tasks: stopped
containers are started, missing containers are recreated from their stored
spec (image, ports, non-secret env), and the summary is reported on the
next heartbeat. A corrupt `state.json` is quarantined aside, never fatal.

Also know: control-plane outages don't hot-spin the worker — heartbeat and
claim failures back off exponentially (5s → 300s cap, jittered) until the
plane is reachable again; and a container that restarts
`WORKER_CRASH_LOOP_THRESHOLD` times (default 5) inside
`WORKER_CRASH_LOOP_WINDOW_S` (default 300s) is stopped and flagged
(`crash_loop`) rather than restarted forever — redeploy or clear the flag
to recover it.

## Public ingress (optional, Phase 7)

Disabled by default. To serve public hostnames from this host through a
worker-managed Cloudflare tunnel (outbound-only — the host dials out, no
inbound ports):

1. Create the tunnel in the Cloudflare dashboard (**Zero Trust → Networks
   → Tunnels → Create a tunnel**), copy the **tunnel token**, note the
   tunnel hostname (`<tunnel-id>.cfargotunnel.com`), and add each public
   hostname in the tunnel's **Public hostnames** tab (required for
   dashboard-created token tunnels).
2. On the control plane, set `CLOUDFLARE_API_TOKEN`,
   `CLOUDFLARE_ZONE_ID`, and `TUNNEL_INGRESS_HOSTNAME=<tunnel-id>.cfargotunnel.com`.
3. On the host, add to `/opt/agent-host/config/worker.env` (mode `0600`):

```bash
WORKER_INGRESS_ENABLED=true
WORKER_INGRESS_PROVIDER=cloudflare-tunnel
UAHT_TUNNEL_TOKEN=<tunnel token from step 1>   # never in the repo, never logged
```

   (or pass `UAHT_INGRESS_ENABLED=1 UAHT_TUNNEL_TOKEN=...` to
   `scripts/install-host.sh` at install time)
4. Restart the worker, then request domains with `--ingress tunnel`:

```bash
agent-host domains add --deployment <id> --hostname api.example.com --ingress tunnel
```

The worker downloads `cloudflared` (pinned release, SHA-256 verified) if
needed, starts the supervised tunnel, and syncs `hostname →
http://127.0.0.1:<container-port>` routes on every tunnel-mode domain
change. Full story (including the honest limits): `docs/cloudflare.md`.

## Updating

The worker self-updates: the control plane advertises the current worker
version and the worker pulls and applies updates on its own schedule
(emits `worker.updated`). To force an update, restart the service —
`sudo systemctl restart agent-host-worker.service`. The updater health-gates
the restart: the new worker must write a boot marker and look healthy
within `WORKER_UPDATE_HEALTH_TIMEOUT_S` (default 120s), otherwise the
updater rolls back to the previous release.

## Uninstall

```bash
sudo systemctl disable --now agent-host-worker.service
sudo rm /etc/systemd/system/agent-host-worker.service
sudo systemctl daemon-reload
sudo rm -rf /opt/agent-host /srv/agent-apps /etc/uagt
# Optionally deregister the host row via the control plane API.
```

The token in `/opt/agent-host/config/worker.env` is deleted with the
directory. If the token may have leaked, rotate it
(`POST /v1/hosts/:id/rotate-token` with the host token) or re-register the
host (the old token stops working).
