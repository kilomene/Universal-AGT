#!/usr/bin/env bash
#
# check-build-consistency.sh — source/build/version coherence checks (§1, §61).
#
# Fails on:
#   1. Version drift: the single source of truth for each component's
#      version must match the version declared in its packaging metadata:
#        * control-plane/api:  src/lib/versions.ts API_VERSION
#                              == api/package.json "version"
#        * agent-sdk/javascript: src/client.js must derive its default
#                              User-Agent from package.json (no hardcoded
#                              version literal), and package.json must carry
#                              a semver "version"
#        * agent-sdk/python:   src/uaht_sdk/client.py __version__
#                              == pyproject.toml version
#        * cli:                pyproject.toml version is a valid semver
#                              (the --version flag serves it via
#                              importlib.metadata at runtime)
#        * host-worker:        agent/config.py worker_version default is a
#                              valid semver and is not newer than the API's
#                              MIN_WORKER_VERSION floor accepts
#                              (i.e. the shipped worker registers cleanly)
#   2. Committed generated output: dist/, build/, node_modules/,
#      __pycache__/, .venv/, *.egg-info/, *.tsbuildinfo must never be
#      tracked in git (they are rebuilt from source on every install).
#   3. .gitignore coverage: the repo-root .gitignore must exclude dist/
#      (the control-plane build output).
#
# Usage: scripts/check-build-consistency.sh [ROOT]
#   ROOT defaults to the enclosing git work tree, else the current directory.
# Exit 0: coherent. Exit 1: drift detected.
set -euo pipefail

ROOT="${1:-}"
if [ -z "$ROOT" ]; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi
cd "$ROOT"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
ok()   { echo "ok: $*"; }

SEMVER_RE='^[0-9]+\.[0-9]+\.[0-9]+$'

# ---------------------------------------------------------------------------
# 1. Version coherence
# ---------------------------------------------------------------------------
AHP="agent-host-platform"

# --- control-plane/api: versions.ts API_VERSION == package.json version ---
API_PKG="$AHP/control-plane/api/package.json"
API_VERSIONS_TS="$AHP/control-plane/api/src/lib/versions.ts"
pkg_version="$(python3 -c "import json;print(json.load(open('$API_PKG'))['version'])")"
ts_version="$(grep -E "^export const API_VERSION" "$API_VERSIONS_TS" | sed -E "s/.*'([^']+)'.*/\1/" || true)"
ts_min_worker="$(grep -E "^export const MIN_WORKER_VERSION" "$API_VERSIONS_TS" | sed -E "s/.*'([^']+)'.*/\1/" || true)"
if [[ "$pkg_version" != "$ts_version" ]]; then
  fail "control-plane API version drift: package.json=$pkg_version versions.ts API_VERSION=$ts_version"
else
  ok "control-plane API version $pkg_version (package.json == versions.ts)"
fi
if ! [[ "$ts_min_worker" =~ $SEMVER_RE ]]; then
  fail "MIN_WORKER_VERSION is not semver: $ts_min_worker"
else
  ok "MIN_WORKER_VERSION=$ts_min_worker"
fi

# --- agent-sdk/javascript: no hardcoded version in the default User-Agent ---
JS_PKG="$AHP/agent-sdk/javascript/package.json"
JS_CLIENT="$AHP/agent-sdk/javascript/src/client.js"
js_version="$(python3 -c "import json;print(json.load(open('$JS_PKG'))['version'])")"
if ! [[ "$js_version" =~ $SEMVER_RE ]]; then
  fail "JS SDK package.json version is not semver: $js_version"
fi
if grep -Eq "uaht-sdk/[0-9]+\.[0-9]+\.[0-9]+" "$JS_CLIENT"; then
  fail "JS SDK client.js hardcodes a version literal in the User-Agent (must derive from package.json)"
else
  ok "JS SDK $js_version (client.js derives User-Agent from package.json)"
fi
if ! grep -q "package.json" "$JS_CLIENT"; then
  fail "JS SDK client.js does not read its version from package.json"
fi

# --- agent-sdk/python: client.__version__ == pyproject version --------------
# Single source of truth is src/uaht_sdk/client.py (__version__); __init__.py
# re-exports it and must not define its own.
PY_INIT="$AHP/agent-sdk/python/src/uaht_sdk/__init__.py"
PY_CLIENT_MOD="$AHP/agent-sdk/python/src/uaht_sdk/client.py"
PY_PROJ="$AHP/agent-sdk/python/pyproject.toml"
py_mod_version="$(grep -E '^__version__ = ' "$PY_CLIENT_MOD" | sed -E 's/.*"([^"]+)".*/\1/' || true)"
py_proj_version="$(grep -E '^version = ' "$PY_PROJ" | head -1 | sed -E 's/.*"([^"]+)".*/\1/' || true)"
if [[ -z "$py_mod_version" ]]; then
  fail "Python SDK client.py defines no __version__ (it is the single source of truth)"
elif [[ "$py_mod_version" != "$py_proj_version" ]]; then
  fail "Python SDK version drift: pyproject=$py_proj_version client.__version__=$py_mod_version"
else
  ok "Python SDK version $py_proj_version (pyproject == client.__version__)"
fi
if grep -Eq '^__version__ = ' "$PY_INIT"; then
  fail "Python SDK __init__.py defines its own __version__ (must re-export from .client)"
else
  ok "Python SDK __init__.py re-exports __version__ (no duplicate)"
fi

# --- cli: pyproject version is semver; --version is wired ---
CLI_PROJ="$AHP/cli/pyproject.toml"
cli_version="$(grep -E '^version = ' "$CLI_PROJ" | head -1 | sed -E 's/.*"([^"]+)".*/\1/' || true)"
if ! [[ "$cli_version" =~ $SEMVER_RE ]]; then
  fail "CLI pyproject version is not semver: $cli_version"
else
  ok "CLI version $cli_version"
fi
if ! grep -q 'action="version"' "$AHP/cli/src/agent_host_cli/main.py"; then
  fail "CLI main.py has no --version flag"
else
  ok "CLI --version flag present"
fi

# --- host-worker: worker_version default is semver and satisfies the API floor ---
WORKER_CONFIG="$AHP/host-worker/agent/config.py"
worker_version="$(grep -E '^\s*worker_version:\s*str\s*=' "$WORKER_CONFIG" | head -1 | sed -E 's/.*"([^"]+)".*/\1/' || true)"
if ! [[ "$worker_version" =~ $SEMVER_RE ]]; then
  fail "host-worker worker_version default is not semver: $worker_version"
else
  ok "host-worker version $worker_version"
fi
# numeric floor comparison: worker_version >= MIN_WORKER_VERSION
older="$(python3 - "$worker_version" "$ts_min_worker" <<'PYEOF'
import sys
def parts(v): return [int(x) for x in v.split('.')]
a, b = parts(sys.argv[1]), parts(sys.argv[2])
n = max(len(a), len(b))
a += [0] * (n - len(a)); b += [0] * (n - len(b))
print("yes" if a < b else "no")
PYEOF
)"
if [[ "$older" == "yes" ]]; then
  fail "host-worker $worker_version is below the API MIN_WORKER_VERSION $ts_min_worker — a fresh worker would be rejected at registration"
else
  ok "host-worker $worker_version satisfies API floor $ts_min_worker"
fi

# ---------------------------------------------------------------------------
# 2. No committed generated output
# ---------------------------------------------------------------------------
GEN_RE='(^|/)(dist|build|node_modules|__pycache__|\.venv)(/|$)|(^|/)[^/]*\.egg-info(/|$)|\.tsbuildinfo$'
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  tracked="$(git ls-files | grep -E "$GEN_RE" || true)"
  if [[ -n "$tracked" ]]; then
    fail "generated build output is tracked in git:"$'\n'"$tracked"
  else
    ok "no generated build output tracked in git"
  fi
else
  echo "note: not a git work tree — skipping tracked-output check" >&2
fi

# ---------------------------------------------------------------------------
# 3. .gitignore covers the control-plane build output
# ---------------------------------------------------------------------------
if grep -Eq '^\s*dist/\s*$' .gitignore; then
  ok ".gitignore excludes dist/"
else
  fail ".gitignore does not exclude dist/"
fi

echo
if [[ "$failures" -gt 0 ]]; then
  echo "$failures build-consistency check(s) FAILED" >&2
  exit 1
fi
echo "all build-consistency checks passed"
