#!/usr/bin/env bash
# ============================================================================
# Universal-AGT — full production stack installer (one command, zero prompts)
#
# Installs the ENTIRE production stack on a Debian/Ubuntu persistent host
# (the Grok VM) with NO systemd: control-plane (Node) + host-worker (Python)
# + cloudflared tunnel + dockerd, all supervised by supervisord, with a
# cron @reboot hook so a VM update/reboot can never leave everything down
# again.
#
# This script is FULLY NON-INTERACTIVE: it never prompts for anything.
# Secrets are read from their existing on-host locations (/etc/uagt/) and
# are NEVER written into the repo, logs, or chat. If a required secret file
# is missing the script fails loudly with the exact path to restore —
# it does not ask, because there is nothing to type: the values were set up
# on the host already.
#
# Outbound-only design: nothing here opens an inbound port and nothing uses
# Tailscale. The host dials out (control plane -> Supabase, worker ->
# localhost control plane, cloudflared -> Cloudflare edge).
#
# Usage (on the host, as the box user — NOT root, NOT over Tailscale):
#   git clone https://github.com/kilomene/Universal-AGT.git
#   cd Universal-AGT
#   bash install.sh
#
# Idempotent: safe to re-run after a host update/reboot wiped the runtime.
# ============================================================================
set -euo pipefail

# --------------------------------------------------------------------------
# 0. Preconditions (fail fast, fail precisely)
# --------------------------------------------------------------------------
if [ "$(uname -s)" != "Linux" ]; then
  echo "FATAL: this installer runs on Linux only (got $(uname -s))." >&2
  exit 1
fi
if [ "$(id -un)" = "root" ]; then
  echo "FATAL: run as the box user, not root (the script uses sudo itself)." >&2
  exit 1
fi
if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
  echo "FATAL: the box user needs passwordless sudo for apt/docker setup." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$REPO_DIR/agent-host-platform/control-plane/api"
WORKER_DIR="$REPO_DIR/agent-host-platform/host-worker"
for d in "$API_DIR" "$WORKER_DIR"; do
  [ -d "$d" ] || { echo "FATAL: expected repo dir missing: $d (run from the Universal-AGT checkout)." >&2; exit 1; }
done

SUPERVISORD_BIN="$HOME/.local/bin/supervisord"
SUPERVISORCTL_BIN="$HOME/.local/bin/supervisorctl"
SUPERVISOR_CONF="/opt/uaht/supervisor/supervisord.conf"
CLOUDFLARED_BIN="/usr/local/bin/cloudflared"
CLOUDFLARED_YML="/opt/uaht/supervisor/cloudflared.yml"

# --------------------------------------------------------------------------
# 1. Secrets must already exist on the host — never prompt, never invent.
# --------------------------------------------------------------------------
echo "[install] 1. checking required secrets in /etc/uagt/"
for f in /etc/uagt/tunnel.token /etc/uagt/control-plane.env /etc/uagt/worker.env; do
  if [ ! -s "$f" ]; then
    echo "FATAL: required secret file missing or empty: $f" >&2
    echo "       Restore it on this host (it was set up here before the update;" >&2
    echo "       a backup may exist at ~/workspace/.uaht-secrets/). This script" >&2
    echo "       never prompts for secrets and never stores them in the repo." >&2
    exit 1
  fi
done
# Validate the critical keys are non-empty (without printing values).
check_key() { # $1=file $2=key
  if ! grep -qE "^${2}=[^[:space:]]" "$1"; then
    echo "FATAL: $2 is missing or empty in $1" >&2; exit 1
  fi
}
check_key /etc/uagt/control-plane.env DATABASE_URL
check_key /etc/uagt/control-plane.env DATA_ENCRYPTION_KEY
check_key /etc/uagt/control-plane.env UAHT_PROVISIONING_TOKEN
check_key /etc/uagt/worker.env WORKER_HOST_TOKEN
check_key /etc/uagt/worker.env WORKER_HOST_ID
check_key /etc/uagt/worker.env WORKER_CONTROL_PLANE_URL
echo "       secrets present."

# --------------------------------------------------------------------------
# 2. System dependencies (apt)
# --------------------------------------------------------------------------
echo "[install] 2. installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  docker.io python3 python3-pip python3-venv nodejs npm curl cron sudo ca-certificates \
  >/dev/null
node --version
python3 --version

# --------------------------------------------------------------------------
# 3. supervisord (user install, matches the audited production layout)
# --------------------------------------------------------------------------
echo "[install] 3. installing supervisord"
if [ ! -x "$SUPERVISORD_BIN" ]; then
  python3 -m pip install --user --quiet supervisor
fi
"$SUPERVISORD_BIN" --version

# --------------------------------------------------------------------------
# 4. cloudflared binary
# --------------------------------------------------------------------------
echo "[install] 4. installing cloudflared"
if [ ! -x "$CLOUDFLARED_BIN" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) CF_ARCH="amd64" ;; aarch64|arm64) CF_ARCH="arm64" ;;
    *) echo "FATAL: unsupported arch for cloudflared: $ARCH" >&2; exit 1 ;;
  esac
  curl -fsSL -o /tmp/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
  sudo install -m 0755 /tmp/cloudflared "$CLOUDFLARED_BIN"
  rm -f /tmp/cloudflared
fi
"$CLOUDFLARED_BIN" --version

# --------------------------------------------------------------------------
# 5. Docker daemon config: vfs driver (rootfs is overlay; overlay-on-overlay
#    fails). dockerd itself runs under supervisord (no systemd on this host),
#    so make sure no systemd docker unit fights it.
# --------------------------------------------------------------------------
echo "[install] 5. configuring docker (vfs storage driver)"
sudo mkdir -p /etc/docker
echo '{"storage-driver": "vfs"}' | sudo tee /etc/docker/daemon.json >/dev/null
sudo systemctl disable --now docker.service docker.socket 2>/dev/null || true

# --------------------------------------------------------------------------
# 6. Layout: /opt/uaht, secrets perms, artifacts dir
# --------------------------------------------------------------------------
echo "[install] 6. creating /opt/uaht layout"
sudo mkdir -p /opt/uaht/supervisor /opt/uaht/artifacts /etc/uagt
sudo chown -R "$(id -un):$(id -gn)" /opt/uaht
chmod 700 /etc/uagt
sudo chmod 600 /etc/uagt/tunnel.token /etc/uagt/control-plane.env /etc/uagt/worker.env
sudo chown "$(id -un):$(id -gn)" /etc/uagt/tunnel.token /etc/uagt/control-plane.env /etc/uagt/worker.env
# docker group for the box user (dockerd socket access)
sudo groupadd docker 2>/dev/null || true
sudo usermod -aG docker "$(id -un)" || true

# --------------------------------------------------------------------------
# 7. supervisord.conf (generated; secrets stay in /etc/uagt, never here)
# --------------------------------------------------------------------------
echo "[install] 7. writing supervisord.conf"
cat > "$SUPERVISOR_CONF" <<EOF
; Generated by Universal-AGT install.sh — do not hand-edit.
; Secrets live in /etc/uagt/ and are loaded at process start, never here.
[unix_http_server]
file=/opt/uaht/supervisor/supervisor.sock
chmod=0700

[supervisord]
logfile=/opt/uaht/supervisor/supervisord.log
pidfile=/opt/uaht/supervisor/supervisord.pid
childlogdir=/opt/uaht/supervisor
nodaemon=false

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///opt/uaht/supervisor/supervisor.sock

[program:control-plane]
directory=$API_DIR
command=/bin/bash -c 'set -a; source /etc/uagt/control-plane.env; set +a; exec /usr/bin/node dist/index.js'
user=$(id -un)
autostart=true
autorestart=true
startsecs=10
stdout_logfile=/opt/uaht/supervisor/control-plane.log
stderr_logfile=/opt/uaht/supervisor/control-plane.err.log

[program:host-worker]
directory=$WORKER_DIR
command=/usr/bin/python3 -m agent.main --config /etc/uagt/worker.env
user=$(id -un)
autostart=true
autorestart=true
startsecs=10
stdout_logfile=/opt/uaht/supervisor/host-worker.log
stderr_logfile=/opt/uaht/supervisor/host-worker.err.log

[program:cloudflared]
command=/bin/bash -c 'exec $CLOUDFLARED_BIN tunnel --config $CLOUDFLARED_YML --no-autoupdate run --token \$(cat /etc/uagt/tunnel.token)'
user=$(id -un)
autostart=true
autorestart=true
startsecs=10
stdout_logfile=/opt/uaht/supervisor/cloudflared.log
stderr_logfile=/opt/uaht/supervisor/cloudflared.err.log

[program:dockerd]
command=/usr/bin/sudo /usr/bin/dockerd
user=$(id -un)
autostart=true
autorestart=true
startsecs=10
stdout_logfile=/opt/uaht/supervisor/dockerd.log
stderr_logfile=/opt/uaht/supervisor/dockerd.err.log
EOF
chmod 600 "$SUPERVISOR_CONF"

cat > "$CLOUDFLARED_YML" <<'EOF'
# cloudflared tunnel client config. Ingress rules live in the Cloudflare
# dashboard for this token tunnel (public hostname -> http://127.0.0.1:3000).
no-autoupdate: true
EOF
chmod 600 "$CLOUDFLARED_YML"

# --------------------------------------------------------------------------
# 8. Build control plane + worker deps
# --------------------------------------------------------------------------
echo "[install] 8. building control plane"
cd "$API_DIR"
npm ci --no-audit --no-fund >/dev/null 2>&1 || npm install --no-audit --no-fund >/dev/null
npm run build
echo "[install] 9. installing worker python deps"
cd "$WORKER_DIR"
python3 -m pip install --user --quiet -r requirements.txt

# --------------------------------------------------------------------------
# 9. cron @reboot hook (the fix for the update-reboot outage)
# --------------------------------------------------------------------------
echo "[install] 10. installing cron @reboot hook"
CRON_LINE="@reboot $SUPERVISORD_BIN -c $SUPERVISOR_CONF"
( crontab -l 2>/dev/null | grep -v -F "supervisord -c $SUPERVISOR_CONF" ; echo "$CRON_LINE" ) | crontab -
sudo service cron start 2>/dev/null || sudo systemctl start cron 2>/dev/null || true

# --------------------------------------------------------------------------
# 10. (Re)start supervisord and verify
# --------------------------------------------------------------------------
echo "[install] 11. starting supervisord"
"$SUPERVISORCTL_BIN" -c "$SUPERVISOR_CONF" shutdown 2>/dev/null || true
sleep 2
"$SUPERVISORD_BIN" -c "$SUPERVISOR_CONF"
sleep 12

echo "[install] 12. verifying services"
"$SUPERVISORCTL_BIN" -c "$SUPERVISOR_CONF" status
NOT_RUNNING="$("$SUPERVISORCTL_BIN" -c "$SUPERVISOR_CONF" status | grep -v RUNNING || true)"
if [ -n "$NOT_RUNNING" ]; then
  echo "FATAL: not all services are RUNNING:" >&2
  echo "$NOT_RUNNING" >&2
  exit 1
fi

# dockerd needs a moment before `docker info` works
for i in $(seq 1 12); do
  docker info >/dev/null 2>&1 && break
  sleep 5
done
docker info >/dev/null 2>&1 || { echo "FATAL: dockerd did not come up (see /opt/uaht/supervisor/dockerd.err.log)." >&2; exit 1; }
echo "       docker OK: $(docker info --format '{{.ServerVersion}}' 2>/dev/null)"

# local control-plane health
for i in $(seq 1 12); do
  curl -sf -m 5 http://127.0.0.1:3000/v1/health >/dev/null 2>&1 && break
  sleep 5
done
HEALTH="$(curl -sf -m 10 http://127.0.0.1:3000/v1/health || true)"
if [ -z "$HEALTH" ]; then
  echo "FATAL: control plane health check failed (see /opt/uaht/supervisor/control-plane.err.log)." >&2
  exit 1
fi
echo "       control plane health: $HEALTH"

# tunnel registration (cloudflared -> Cloudflare edge)
for i in $(seq 1 12); do
  grep -q "Registered tunnel connection" /opt/uaht/supervisor/cloudflared.log 2>/dev/null && break
  sleep 5
done
if grep -q "Registered tunnel connection" /opt/uaht/supervisor/cloudflared.log 2>/dev/null; then
  echo "       cloudflared: tunnel registered with Cloudflare edge."
else
  echo "FATAL: cloudflared did not register the tunnel (see /opt/uaht/supervisor/cloudflared.err.log)." >&2
  exit 1
fi

echo ""
echo "OK: Universal-AGT production stack is up."
echo "    Verify public HTTPS from anywhere: curl https://uaht.novamail.store/v1/health"
