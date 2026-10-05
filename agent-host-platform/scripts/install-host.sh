#!/usr/bin/env bash
# ============================================================================
# Universal AGT — persistent host installer (one command)
#
# Installs the host worker on a Debian/Ubuntu persistent Linux host:
#   * fails unless running on Linux, as root, with python3 >= 3.10
#   * installs python3 + python3-requests + docker.io if missing (apt);
#     falls back to `pip install -r host-worker/requirements.txt` when the
#     requests module is still not importable
#   * validates the Docker daemon with `docker info` (hard failure if down)
#   * creates the `agenthost` system user (added to the docker group)
#   * lays out /opt/agent-host and /srv/agent-apps
#   * copies the full worker tree (agent, executor, deployments, docker,
#     health, logs, updater, ingress) to /opt/agent-host/worker
#   * provisions the host at the control plane (POST /v1/hosts/register)
#     unless UAHT_HOST_TOKEN / UAHT_HOST_ID are already supplied
#   * writes /opt/agent-host/config/worker.env (0600, agenthost-owned)
#   * installs + enables + starts the agent-host-worker systemd unit
#   * verifies, in order, failing loudly on any failure:
#       1. `systemctl is-active agent-host-worker`
#       2. the worker's own `--self-check` (run as agenthost): config
#          validates, deployment state readable, docker reachable-or-skipped
#       3. a first "heartbeat ok" line in the unit journal within 120s
#
# Usage:
#   sudo UAHT_CONTROL_PLANE_URL=https://cp.example.com \
#        UAHT_HOST_NAME=persistent-host-01 \
#        ./scripts/install-host.sh
#
# Optional env:
#   UAHT_HOST_TOKEN / UAHT_HOST_ID   use pre-provisioned credentials
#                                     (both or neither; else we register)
#   UAHT_WORKER_VERSION               advertised worker version (default 0.1.0)
#   UAHT_REPO_DIR                     path to the Universal-AGT checkout
#                                     (default: parent of this script's repo)
#   UAHT_SKIP_APT=1                   skip apt installs (deps pre-installed)
#   UAHT_INGRESS_ENABLED=1            enable worker-managed public ingress
#                                     (Phase 7; default: disabled)
#   UAHT_TUNNEL_TOKEN=<token>         cloudflared tunnel token from the
#                                     Cloudflare dashboard (required when
#                                     ingress is enabled)
#
# The worker is OUTBOUND ONLY: it opens HTTPS to the control plane and never
# listens on any port. No inbound firewall rules, no tunnels, no IPs needed.
#
# ----------------------------------------------------------------------------
# MANUAL / OFFLINE INSTALL (no apt, no network at install time)
# ----------------------------------------------------------------------------
# 1. On a machine with network: download python3, python3-requests and
#    docker.io .debs (and their dependencies) for the target arch, plus copy
#    this repo's agent-host-platform/host-worker tree.
# 2. On the target host (as root):
#      dpkg -i *.deb
#      useradd --system --no-create-home --shell /usr/sbin/nologin agenthost
#      usermod -aG docker agenthost
#      mkdir -p /opt/agent-host/config /opt/agent-host/worker /srv/agent-apps
#      cp -r <repo>/agent-host-platform/host-worker/{agent,executor,\
#        deployments,docker,health,logs,updater,ingress} /opt/agent-host/worker/
#      python3 -m pip install --break-system-packages \
#        -r <repo>/agent-host-platform/host-worker/requirements.txt
#      chown -R agenthost:agenthost /opt/agent-host /srv/agent-apps
# 3. Write /opt/agent-host/config/worker.env by hand (see host-worker/
#    .env.example), mode 0600, owned by agenthost. To provision credentials
#    without the installer, POST /v1/hosts/register from any machine:
#      curl -s -X POST $UAHT_CONTROL_PLANE_URL/v1/hosts/register \
#        -H 'Content-Type: application/json' \
#        -d '{"name":"persistent-host-01","worker_version":"0.1.0"}'
#    and store the returned host.id / host_token as WORKER_HOST_ID and
#    WORKER_HOST_TOKEN.
# 4. Copy agent-host-platform/host-worker/agent/agent-host-worker.service to
#    /etc/systemd/system/, then:
#      systemctl daemon-reload
#      systemctl enable --now agent-host-worker
#      journalctl -u agent-host-worker -f   # watch for "heartbeat ok"
# ============================================================================
set -euo pipefail

# ---- config ----------------------------------------------------------------
UAHT_CONTROL_PLANE_URL="${UAHT_CONTROL_PLANE_URL:-}"
UAHT_HOST_NAME="${UAHT_HOST_NAME:-}"
UAHT_HOST_TOKEN="${UAHT_HOST_TOKEN:-}"
UAHT_HOST_ID="${UAHT_HOST_ID:-}"
UAHT_WORKER_VERSION="${UAHT_WORKER_VERSION:-0.1.0}"
UAHT_SKIP_APT="${UAHT_SKIP_APT:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UAHT_REPO_DIR="${UAHT_REPO_DIR:-$REPO_ROOT}"
WORKER_SRC="$UAHT_REPO_DIR/host-worker"

CONFIG_DIR="/opt/agent-host/config"
WORKER_DIR="/opt/agent-host/worker"
APPS_DIR="/srv/agent-apps"
ENV_FILE="$CONFIG_DIR/worker.env"
SERVICE_NAME="agent-host-worker"
WORKER_USER="agenthost"

# Every first-party worker package the installer must lay down. The worker's
# entry point (agent/main.py) imports all of these at startup; a package
# missing here means ModuleNotFoundError on the first boot.
WORKER_PACKAGES="agent executor deployments docker health logs updater ingress"

log()  { echo "[install-host] $*"; }
fail() { echo "[install-host] ERROR: $*" >&2; exit 1; }

# ---- preconditions ----------------------------------------------------------
[ "$(id -u)" -eq 0 ] || fail "run as root (sudo)"
[ "$(uname -s)" = "Linux" ] || fail "this installer only supports Linux (detected: $(uname -s))"
[ -n "$UAHT_CONTROL_PLANE_URL" ] || fail "UAHT_CONTROL_PLANE_URL is required"
[ -n "$UAHT_HOST_NAME" ] || fail "UAHT_HOST_NAME is required (e.g. persistent-host-01)"
if [ -n "$UAHT_HOST_TOKEN" ] && [ -z "$UAHT_HOST_ID" ]; then
  fail "UAHT_HOST_TOKEN given without UAHT_HOST_ID (supply both or neither)"
fi
if [ -n "$UAHT_HOST_ID" ] && [ -z "$UAHT_HOST_TOKEN" ]; then
  fail "UAHT_HOST_ID given without UAHT_HOST_TOKEN (supply both or neither)"
fi
[ -d "$WORKER_SRC/agent" ] || fail "worker source not found at $WORKER_SRC"
[ -f "$WORKER_SRC/requirements.txt" ] || fail "requirements.txt not found at $WORKER_SRC/requirements.txt"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) log "detected OS: $PRETTY_NAME";;
    *) log "WARNING: untested distro ($PRETTY_NAME); apt steps target Debian/Ubuntu and may fail";;
  esac
else
  log "WARNING: /etc/os-release missing; apt steps target Debian/Ubuntu"
fi

# ---- dependencies -----------------------------------------------------------
if [ "$UAHT_SKIP_APT" != "1" ]; then
  command -v apt-get >/dev/null || fail "apt-get not found and UAHT_SKIP_APT != 1"
  log "installing dependencies (python3, python3-requests, docker.io)"
  apt-get update -qq
  for pkg in python3 python3-requests docker.io; do
    dpkg -s "$pkg" >/dev/null 2>&1 || apt-get install -y -qq "$pkg"
  done
else
  log "UAHT_SKIP_APT=1: skipping apt installs"
fi
command -v python3 >/dev/null || fail "python3 still missing after install step"
python3 - <<'EOF' || fail "python3 >= 3.10 is required (worker uses 3.10+ syntax)"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
log "python3 version OK: $(python3 -c 'import sys; print(sys.version.split()[0])')"

# The worker needs the `requests` library (agent/api.py). Prefer the distro
# package installed above; fall back to pip installing requirements.txt.
if ! python3 -c "import requests" 2>/dev/null; then
  log "requests module missing; installing from $WORKER_SRC/requirements.txt"
  PIP_CMD=(pip3)
  if ! command -v pip3 >/dev/null 2>&1; then
    python3 -m pip --version >/dev/null 2>&1 \
      || fail "pip is unavailable; install python3-requests or python3-pip and re-run"
    PIP_CMD=(python3 -m pip)
  fi
  "${PIP_CMD[@]}" install --break-system-packages -r "$WORKER_SRC/requirements.txt" \
    || fail "pip install -r requirements.txt failed"
  python3 -c "import requests" 2>/dev/null \
    || fail "requests still not importable after pip install"
fi

# The worker schedules real Docker workloads: the daemon must be up, not
# merely installed.
docker info >/dev/null 2>&1 \
  || fail "docker daemon is not running ('docker info' failed); start Docker and re-run"

# ---- user / directories ------------------------------------------------------
if ! id "$WORKER_USER" >/dev/null 2>&1; then
  log "creating system user $WORKER_USER"
  useradd --system --no-create-home --shell /usr/sbin/nologin "$WORKER_USER"
fi
if getent group docker >/dev/null && ! id -nG "$WORKER_USER" | grep -qw docker; then
  usermod -aG docker "$WORKER_USER"
  log "added $WORKER_USER to docker group"
fi

mkdir -p "$CONFIG_DIR" "$WORKER_DIR" "$APPS_DIR" \
         /opt/agent-host/{logs,deployments,artifacts,builds,updates,releases,quarantine}
# copy worker code (idempotent: remove-then-copy per package)
log "installing worker code to $WORKER_DIR"
# shellcheck disable=SC2086
for pkg in $WORKER_PACKAGES; do
  [ -d "$WORKER_SRC/$pkg" ] || fail "worker package missing from source: $WORKER_SRC/$pkg"
  rm -rf "$WORKER_DIR/$pkg"
  cp -r "$WORKER_SRC/$pkg" "$WORKER_DIR/$pkg"
done
chown -R "$WORKER_USER:$WORKER_USER" /opt/agent-host "$APPS_DIR"
chmod 0750 /opt/agent-host "$APPS_DIR"

# ---- provisioning -------------------------------------------------------------
if [ -z "$UAHT_HOST_TOKEN" ]; then
  log "provisioning host at $UAHT_CONTROL_PLANE_URL (POST /v1/hosts/register)"
  PROVISION_JSON="$(python3 - "$UAHT_CONTROL_PLANE_URL" "$UAHT_HOST_NAME" "$UAHT_WORKER_VERSION" <<'EOF'
import json, sys, urllib.request
base, name, version = sys.argv[1], sys.argv[2], sys.argv[3]
body = json.dumps({"name": name, "host_type": "persistent-linux-host",
                   "capabilities": ["docker", "docker-compose"],
                   "worker_version": version}).encode()
req = urllib.request.Request(base.rstrip("/") + "/v1/hosts/register",
                             data=body,
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.load(resp)
host, token = data["host"], data["host_token"]
print(json.dumps({"host_id": host["id"], "host_token": token}))
EOF
)"
  UAHT_HOST_ID="$(python3 -c "import json,sys; print(json.load(sys.stdin)['host_id'])" <<<"$PROVISION_JSON")"
  UAHT_HOST_TOKEN="$(python3 -c "import json,sys; print(json.load(sys.stdin)['host_token'])" <<<"$PROVISION_JSON")"
  [ -n "$UAHT_HOST_ID" ] && [ -n "$UAHT_HOST_TOKEN" ] \
    || fail "provisioning failed: empty host_id/token in response"
  log "provisioned host_id=$UAHT_HOST_ID"
else
  log "using pre-provisioned credentials"
fi

# ---- worker.env ---------------------------------------------------------------
log "writing $ENV_FILE"
umask 077
cat > "$ENV_FILE" <<EOF
# Generated by install-host.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Do not commit.
WORKER_CONTROL_PLANE_URL=$UAHT_CONTROL_PLANE_URL
WORKER_HOST_NAME=$UAHT_HOST_NAME
WORKER_HOST_TOKEN=$UAHT_HOST_TOKEN
WORKER_HOST_ID=$UAHT_HOST_ID
WORKER_POLL_WAIT=25
WORKER_HEARTBEAT_INTERVAL=30
WORKER_WORK_DIR=/opt/agent-host
WORKER_APPS_DIR=/srv/agent-apps
WORKER_WORKER_VERSION=$UAHT_WORKER_VERSION
# --- optional public ingress (Phase 7; disabled by default) -----------------
# Set UAHT_INGRESS_ENABLED=1 + UAHT_TUNNEL_TOKEN in the installer environment
# to enable the worker-managed cloudflared tunnel (outbound-only).
# UAHT_TUNNEL_TOKEN is the operator input; it is written into worker.env as
# WORKER_TUNNEL_TOKEN (see docs/CONFIG.md).
WORKER_INGRESS_ENABLED=${UAHT_INGRESS_ENABLED:-false}
WORKER_INGRESS_PROVIDER=cloudflare-tunnel
WORKER_TUNNEL_TOKEN=$UAHT_TUNNEL_TOKEN
EOF
chown "$WORKER_USER:$WORKER_USER" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# ---- systemd ------------------------------------------------------------------
UNIT_SRC="$WORKER_SRC/agent/agent-host-worker.service"
[ -f "$UNIT_SRC" ] || fail "systemd unit not found at $UNIT_SRC"
log "installing systemd unit $SERVICE_NAME"
cp "$UNIT_SRC" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload \
  || fail "systemctl daemon-reload failed"
systemctl enable --now "$SERVICE_NAME" \
  || fail "systemctl enable --now $SERVICE_NAME failed"

# ---- verify ---------------------------------------------------------------------
# Never claim success from `systemctl start` alone. Three gates:
#   1. systemd reports the unit active;
#   2. the worker's own --self-check passes as the agenthost user (config
#      loads, deployment state readable, docker reachable-or-skipped, and —
#      crucially — every worker module imports, which catches a broken
#      worker-tree copy);
#   3. a first "heartbeat ok" lands in the journal within 120s, proving the
#      worker can actually reach the control plane.
log "waiting for service to settle..."
sleep 8
systemctl is-active --quiet "$SERVICE_NAME" \
  || fail "service failed to start; see: journalctl -u $SERVICE_NAME -n 100"

SELF_CHECK_CMD="cd '$WORKER_DIR' && exec /usr/bin/python3 -m agent.main --config '$ENV_FILE' --self-check"
if command -v runuser >/dev/null 2>&1; then
  SELF_CHECK_RUN=(runuser -u "$WORKER_USER" -- bash -c "$SELF_CHECK_CMD")
else
  SELF_CHECK_RUN=(su -s /bin/bash "$WORKER_USER" -c "$SELF_CHECK_CMD")
fi
if SELF_CHECK_OUT="$("${SELF_CHECK_RUN[@]}" 2>&1)"; then
  log "worker self-check passed: $SELF_CHECK_OUT"
else
  fail "worker self-check failed as $WORKER_USER (exit != 0): $SELF_CHECK_OUT"
fi

log "waiting for first heartbeat (up to 120s)..."
HEARTBEAT_SEEN=0
for _ in $(seq 1 24); do
  if journalctl -u "$SERVICE_NAME" -n 60 --no-pager 2>/dev/null | grep -q "heartbeat ok"; then
    HEARTBEAT_SEEN=1
    break
  fi
  sleep 5
done
[ "$HEARTBEAT_SEEN" -eq 1 ] \
  || fail "no 'heartbeat ok' in the journal within 120s; the worker cannot reach the control plane at $UAHT_CONTROL_PLANE_URL — inspect: journalctl -u $SERVICE_NAME -n 100"
log "verified: heartbeat reached the control plane"

log "done. Host '$UAHT_HOST_NAME' is registered and the worker is running."
log "Logs: journalctl -u $SERVICE_NAME -f"
log "State: /opt/agent-host (config, logs, deployments)"
