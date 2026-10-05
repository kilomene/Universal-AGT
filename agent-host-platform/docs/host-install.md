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

From any machine that can reach the control plane (bootstrap credentials
depend on your control-plane deployment; the token is shown **once**):

```bash
curl -s -X POST https://control-plane.example.com/v1/hosts/register \
  -H 'Content-Type: application/json' \
  -d '{"name": "persistent-host-01", "host_type": "linux",
       "capabilities": ["docker", "docker-compose"],
       "worker_version": "1.0.0"}'
# → 201 {"host": {...}, "host_token": "<show once>"}
```

Save the token into the host's environment file, not into chat logs or
tickets:

```bash
sudo install -m 0600 /dev/null /etc/uagt/worker.env
sudo tee /etc/uagt/worker.env >/dev/null <<'EOF'
UAGT_CONTROL_PLANE="https://control-plane.example.com"
UAGT_HOST_TOKEN="<paste token here>"
EOF
```

## Manual steps (without the installer)

```bash
# 1. Copy the worker
sudo mkdir -p /opt/uagt/worker
sudo cp -r host-worker/* /opt/uagt/worker/
sudo chmod +x /opt/uagt/worker/worker.py   # entrypoint

# 2. Environment file with secrets (root-only)
sudo mkdir -p /etc/uagt
sudo install -m 0600 /dev/null /etc/uagt/worker.env
# ... write UAGT_CONTROL_PLANE and UAGT_HOST_TOKEN as above ...

# 3. systemd unit
sudo tee /etc/systemd/system/uagt-worker.service >/dev/null <<'EOF'
[Unit]
Description=Universal AGT host worker
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=root
EnvironmentFile=/etc/uagt/worker.env
ExecStart=/usr/bin/python3 /opt/uagt/worker/worker.py
Restart=always
RestartSec=5
# Hardening: the worker only needs outbound HTTPS + docker socket
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/uagt /var/lib/uagt

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now uagt-worker.service
```

## Verification

On the host:

```bash
sudo systemctl status uagt-worker.service --no-pager
sudo journalctl -u uagt-worker.service -n 50 --no-pager   # look for "heartbeat ok"
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

## Updating

The worker self-updates: the control plane advertises the current worker
version and the worker pulls and applies updates on its own schedule
(emits `worker.updated`). To force an update, restart the service —
`sudo systemctl restart uagt-worker.service`.

## Uninstall

```bash
sudo systemctl disable --now uagt-worker.service
sudo rm /etc/systemd/system/uagt-worker.service
sudo systemctl daemon-reload
sudo rm -rf /opt/uagt /etc/uagt /var/lib/uagt
# Optionally deregister the host row via the control plane API.
```

The token in `/etc/uagt/worker.env` is deleted with the directory. If the
token may have leaked, rotate it by re-registering the host (the old token
stops working).
