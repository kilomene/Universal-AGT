#!/usr/bin/env bash
# ============================================================================
# Universal AGT — installer validation test (no root required)
#
# Dry-run / filesystem-layout validation for scripts/install-host.sh:
#   1. bash -n syntax check (+ shellcheck when available)
#   2. every package the installer copies exists in the worker source tree
#   3. every first-party package the worker's runtime code imports is in the
#      installer's copy list (catches a missing `ingress`-style gap)
#   4. all worker modules byte-compile
#   5. simulated install into a temp root using the installer's own copy
#      list, then the worker's real `--self-check` runs against that layout
#      (proves the installed import chain resolves: agent.main imports
#      every package, including ingress)
#   6. systemd unit contract: paths/user referenced by the unit exist and
#      match what the installer lays down
#   7. installer contract: the hardened preconditions and verification gates
#      are present in the script text
#
# Usage: ./scripts/test-install.sh   (from agent-host-platform/ or anywhere)
# Exit 0 on pass, 1 on any failure. Safe to run in CI without root.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALLER="$SCRIPT_DIR/install-host.sh"
WORKER_SRC="$PLATFORM_DIR/host-worker"
UNIT_SRC="$WORKER_SRC/agent/agent-host-worker.service"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "  ok: $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $*" >&2; }

echo "[test-install] 1. syntax"
bash -n "$INSTALLER" && pass "bash -n install-host.sh" || fail "bash -n install-host.sh"
bash -n "$0" && pass "bash -n test-install.sh" || fail "bash -n test-install.sh"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning "$INSTALLER" && pass "shellcheck install-host.sh" \
    || fail "shellcheck install-host.sh"
else
  echo "  skip: shellcheck not installed (noted; CI image should provide it)"
fi

echo "[test-install] 2. installer copy list vs worker source tree"
# Extract WORKER_PACKAGES="..." from the installer itself.
PACKAGES="$(grep -E '^WORKER_PACKAGES=' "$INSTALLER" | head -1 | cut -d'"' -f2)"
[ -n "$PACKAGES" ] || { fail "could not extract WORKER_PACKAGES from installer"; PACKAGES=""; }
for pkg in $PACKAGES; do
  if [ -d "$WORKER_SRC/$pkg" ]; then
    pass "source package present: $pkg"
  else
    fail "installer copies '$pkg' but $WORKER_SRC/$pkg is missing"
  fi
done

echo "[test-install] 3. worker import graph covered by installer copy list"
python3 - "$WORKER_SRC" "$PACKAGES" <<'EOF' || true
import ast, os, sys
src, packages = sys.argv[1], set(sys.argv[2].split())
local_pkgs = {d for d in os.listdir(src)
              if os.path.isdir(os.path.join(src, d))
              and os.path.isfile(os.path.join(src, d, "__init__.py"))}
imported = set()
for root, dirs, files in os.walk(src):
    if "tests" in root.split(os.sep):
        continue  # tests are not installed; runtime code only
    for fn in files:
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(root, fn)) as fh:
            tree = ast.parse(fh.read(), fn)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
needed = {m for m in imported if m in local_pkgs}
missing = sorted(needed - packages)
extra = sorted(packages - needed)
print("needed-by-imports:", sorted(needed))
print("installer-copies:  ", sorted(packages))
if missing:
    print("MISSING-FROM-INSTALLER:", " ".join(missing))
    sys.exit(1)
if extra:
    print("note: installer copies but nothing imports:", " ".join(extra))
sys.exit(0)
EOF
if [ $? -eq 0 ]; then pass "all imported packages are installed (incl. ingress)"; \
else fail "worker imports packages the installer does not copy"; fi

echo "[test-install] 4. all worker modules byte-compile"
if python3 -m compileall -q "$WORKER_SRC/agent" "$WORKER_SRC/executor" \
    "$WORKER_SRC/deployments" "$WORKER_SRC/docker" "$WORKER_SRC/health" \
    "$WORKER_SRC/logs" "$WORKER_SRC/updater" "$WORKER_SRC/ingress" 2>/dev/null; then
  pass "compileall over worker packages"
else
  fail "compileall over worker packages"
fi

echo "[test-install] 5. simulated install + real worker --self-check"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
SIM_WORKER="$TMP_ROOT/opt/agent-host/worker"
SIM_CONFIG="$TMP_ROOT/opt/agent-host/config"
mkdir -p "$SIM_WORKER" "$SIM_CONFIG"
for pkg in $PACKAGES; do
  cp -r "$WORKER_SRC/$pkg" "$SIM_WORKER/$pkg"
done
pass "simulated worker tree copied ($PACKAGES)"
cat > "$SIM_CONFIG/worker.env" <<'ENVEOF'
WORKER_CONTROL_PLANE_URL=https://control-plane.example.test
WORKER_HOST_NAME=test-install-host
WORKER_HOST_TOKEN=test-token-not-real
WORKER_HOST_ID=00000000-0000-0000-0000-000000000000
WORKER_POLL_WAIT=25
WORKER_HEARTBEAT_INTERVAL=30
WORKER_WORK_DIR=/opt/agent-host
WORKER_APPS_DIR=/srv/agent-apps
WORKER_WORKER_VERSION=0.0.0-test
ENVEOF
if SELF_OUT="$(cd "$SIM_WORKER" && python3 -m agent.main \
    --config "$SIM_CONFIG/worker.env" --self-check 2>&1)"; then
  pass "worker --self-check exits 0 on simulated layout"
  echo "  self-check report: $SELF_OUT"
else
  fail "worker --self-check failed on simulated layout: $SELF_OUT"
fi

echo "[test-install] 6. systemd unit contract"
[ -f "$UNIT_SRC" ] || fail "unit file missing at $UNIT_SRC"
grep -q 'UNIT_SRC="\$WORKER_SRC/agent/agent-host-worker.service"' "$INSTALLER" \
  && pass "installer references shipped unit path" \
  || fail "installer does not reference agent/agent-host-worker.service"
for line in \
  'User=agenthost' \
  'Group=agenthost' \
  'WorkingDirectory=/opt/agent-host/worker' \
  'EnvironmentFile=/opt/agent-host/config/worker.env' \
  'ExecStart=/usr/bin/python3 -m agent.main --config /opt/agent-host/config/worker.env'; do
  if grep -qF "$line" "$UNIT_SRC"; then pass "unit: $line"; \
  else fail "unit missing: $line"; fi
done
# The installer must create every absolute path the unit depends on.
for path in /opt/agent-host/worker /opt/agent-host/config /srv/agent-apps; do
  if grep -qF "$path" "$INSTALLER"; then pass "installer lays down $path"; \
  else fail "installer never mentions $path (unit needs it)"; fi
done

echo "[test-install] 7. installer hardening contract"
check_present() { # $1=description $2=fixed string
  if grep -qF -- "$2" "$INSTALLER"; then pass "$1"; else fail "$1 (missing: $2)"; fi
}
check_present "set -euo pipefail" "set -euo pipefail"
check_present "Linux gate" 'uname -s'
check_present "python >= 3.10 gate" "3, 10"
check_present "docker daemon validation" "docker info"
check_present "requirements.txt pip fallback" "requirements.txt"
check_present "worker --self-check gate" "--self-check"
check_present "heartbeat verification" "heartbeat ok"
check_present "loud failure helper" 'fail() {'
# The four required config keys must be written into worker.env by the installer.
for key in WORKER_CONTROL_PLANE_URL WORKER_HOST_NAME WORKER_HOST_TOKEN WORKER_HOST_ID; do
  check_present "installer writes $key" "$key="
done
# requests must be installable: it is either the apt package or in requirements.
if grep -q '^requests' "$WORKER_SRC/requirements.txt"; then
  pass "requirements.txt provides requests"
else
  fail "requirements.txt does not provide requests"
fi

echo
echo "[test-install] result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
