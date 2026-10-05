#!/usr/bin/env bash
# ============================================================================
# Universal AGT — persistent host installer (one command)
#
# Installs the host worker on a Debian/Ubuntu persistent Linux host:
#   * installs python3 + docker.io if missing (apt)
#   * creates the `agenthost` system user
#   * lays out /opt/agent-host and /srv/agent-apps
#   * provisions the host at the control plane (POST /v1/hosts/register)
#     unless UAHT_HOST_TOKEN / UAHT_HOST_ID are already supplied
#   * writes /opt/agent-host/config/worker.env (0600, agenthost-owned)
#   * installs + enables + starts the agent-host-worker systemd unit
#   * verifies the service is active and a heartbeat landed
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

log()  { echo "[install-host] $*"; }
fail() { echo "[install-host] ERROR: $*" >&2; exit 1; }

# ---- preconditions ----------------------------------------------------------
[ "$(id -u)" -eq 0 ] || fail "run as root (sudo)"
[ -n "$UAHT_CONTROL_PLANE_URL" ] || fail "UAHT_CONTROL_PLANE_URL is required"
[ -n "$UAHT_HOST_NAME" ] || fail "UAHT_HOST_NAME is required (e.g. persistent-host-01)"
if [ -n "$UAHT_HOST_TOKEN" ] && [ -z "$UAHT_HOST_ID" ]; then
  fail "UAHT_HOST_TOKEN given without UAHT_HOST_ID (supply both or neither)"
fi
if [ -n "$UAHT_HOST_ID" ] && [ -z "$UAHT_HOST_TOKEN" ]; then
  fail "UAHT_HOST_ID given without UAHT_HOST_TOKEN (supply both or neither)"
fi
[ -d "$WORKER_SRC/agent" ] || fail "worker source not found at $WORKER_SRC"

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) log "detected OS: $PRETTY_NAME";;
    *) log "WARNING: untested OS ($PRETTY_NAME); continuing, apt steps may fail";;
  esac
else
  log "WARNING: /etc/os-release missing; cannot verify Debian/Ubuntu"
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
python3 -c "import requests" 2>/dev/null || fail "python3-requests missing (import requests failed)"

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
# copy worker code (idempotent: rsync-like via cp --remove-destination)
log "installing worker code to $WORKER_DIR"
for pkg in agent executor deployments docker health logs updater; do
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
WORKER_INGRESS_ENABLED=${UAHT_INGRESS_ENABLED:-false}
WORKER_INGRESS_PROVIDER=cloudflare-tunnel
UAHT_TUNNEL_TOKEN=${UAHT_TUNNEL_TOKEN:-}
EOF
chown "$WORKER_USER:$WORKER_USER" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

# ---- systemd ------------------------------------------------------------------
UNIT_SRC="$WORKER_SRC/agent/agent-host-worker.service"
[ -f "$UNIT_SRC" ] || fail "systemd unit not found at $UNIT_SRC"
log "installing systemd unit $SERVICE_NAME"
cp "$UNIT_SRC" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# ---- verify ---------------------------------------------------------------------
log "waiting for service to settle..."
sleep 8
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  fail "service failed to start; see: journalctl -u $SERVICE_NAME -n 100"
fi
if journalctl -u "$SERVICE_NAME" -n 60 --no-pager 2>/dev/null | grep -q "heartbeat ok"; then
  log "verified: heartbeat reached the control plane"
else
  log "WARNING: service is active but no 'heartbeat ok' in the recent journal;"
  log "         check connectivity: journalctl -u $SERVICE_NAME -n 50"
fi

log "done. Host '$UAHT_HOST_NAME' is registered and the worker is running."
log "Logs: journalctl -u $SERVICE_NAME -f"
log "State: /opt/agent-host (config, logs, deployments)"
