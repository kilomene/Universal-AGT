# Universal AGT — Host Worker

The agent that runs on the persistent Linux host. It executes deployment
tasks claimed from the control plane work queue and reports progress back.

**Outbound only.** The worker opens HTTPS connections to the control plane
(heartbeat, long-poll claim, progress reports, artifact downloads). It never
listens on any port, never needs an inbound connection, never uses
Tailscale, and never deals in IP addresses — hosts are known by name only.

## Layout

```
host-worker/
  agent/          config.py, api.py (control-plane client), context.py,
                  policy.py (command allowlist), main.py (main loop),
                  agent-host-worker.service (systemd unit)
  executor/       dispatcher.py (claim -> run -> report, secret scrubbing),
                  handlers.py (one handler per task type)
  deployments/    manifest.py (agent.deploy.json validator),
                  pipeline.py (deploy/rollback), state.py (local registry)
  docker/         client.py (argv-only docker CLI wrapper)
  health/         collector.py (/proc metrics), checker.py (healthchecks)
  logs/           store.py (per-task/deployment logs, rotation + caps)
  updater/        self_update.py (verified self-update + rollback)
  tests/          pytest suite
```

## Install (one command, as root)

```bash
sudo UAHT_CONTROL_PLANE_URL=https://cp.example.com \
     UAHT_HOST_NAME=persistent-host-01 \
     ./scripts/install-host.sh
```

The installer detects Debian/Ubuntu, installs `python3`, `python3-requests`
and `docker.io` via apt, creates the `agenthost` system user, lays out
`/opt/agent-host` and `/srv/agent-apps`, provisions the host at
`POST /v1/hosts/register` (or uses `UAHT_HOST_TOKEN`/`UAHT_HOST_ID` when
supplied), writes `/opt/agent-host/config/worker.env` (0600), installs the
systemd unit, enables + starts it, and verifies a heartbeat landed.

Manual / offline steps are documented in the header of
`agent-host-platform/scripts/install-host.sh`.

## Configuration

`/opt/agent-host/config/worker.env` (or `WORKER_*` env vars, or `--config`):

| Key | Default | Meaning |
|---|---|---|
| `WORKER_CONTROL_PLANE_URL` | — (required) | `https://` control plane base URL |
| `WORKER_HOST_NAME` | — (required) | e.g. `persistent-host-01` |
| `WORKER_HOST_TOKEN` | — (required) | bearer token (provisioned once) |
| `WORKER_HOST_ID` | — (required) | host UUID from registration |
| `WORKER_POLL_WAIT` | 25 | claim long-poll seconds (max 30) |
| `WORKER_HEARTBEAT_INTERVAL` | 30 | heartbeat seconds (min 5) |
| `WORKER_WORK_DIR` | /opt/agent-host | state, logs, deployments |
| `WORKER_APPS_DIR` | /srv/agent-apps | artifacts / app files |
| `WORKER_WORKER_VERSION` | 0.1.0 | advertised version |

## Task execution policy

`agent/policy.py` is the command allowlist. The worker **never** executes an
arbitrary shell string from the network: each of the 17 protocol task types
maps to one fixed Python handler that builds its own argv list
(`subprocess` without `shell=True`). Unknown types are rejected and reported
`failed` with zero execution. Payload filesystem paths are confined to the
work/app dirs. Secret values (payload `secrets`, host token) are scrubbed
from every log line before it is written or streamed.

## Deploy flow (`deploy` task)

1. Download artifact (`GET /v1/artifacts/:id/download`), verify SHA-256 —
   mismatch quarantines the file and fails **before any docker call**.
2. Extract (tar.gz/tgz/tar/zip, traversal-protected), validate
   `agent.deploy.json` per protocol §4 — invalid fails fast.
3. Check host resources vs `manifest.resources`.
4. `docker build` (or `docker compose up --build`, or a static nginx image),
   then `docker run --name uaht-<project>-<version>` with the mapped port,
   env (manifest env + injected secrets), `--memory`/`--cpus`, restart policy.
5. HTTP healthcheck on the mapped port; on failure the new container is
   removed and the previous healthy version is restarted (rollback).
6. State recorded under `/opt/agent-host/deployments/<deployment_id>/`
   (secret env values are never persisted).

## Logs

Per-task and per-deployment logs under `/opt/agent-host/logs/`
(`tasks/<task_id>.log`, `deployments/<deployment_id>.log`), 10 MiB cap per
file with rotation. The service journal also carries stdout:
`journalctl -u agent-host-worker -f`.

## Self-update

The heartbeat response may carry `worker_update: {version, url, sha256}`.
The worker downloads it, verifies SHA-256, extracts to
`/opt/agent-host/releases/<version>/`, runs a compile + import self-check,
swings the `current` symlink, and restarts via systemd — rolling the
symlink back if the restart fails. No-op when no update is advertised.

## Development

```bash
cd agent-host-platform/host-worker
pip install -r requirements.txt
pytest
```
