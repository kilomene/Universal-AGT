#!/usr/bin/env bash
#
# secrets-sweep.sh — fail if secret-shaped material is committed.
#
# Single source of truth for secret scanning:
#   * CI (.github/workflows/ci.yml, `secrets-sweep` job) invokes this script
#     against the checked-out repo (git-tracked files).
#   * The local suite (control-plane/api/test/secretsSweep.test.ts) invokes
#     the SAME script against fixture trees and the repo tree, so the scan
#     is exercised even when CI runners are unavailable.
#
# Usage: scripts/secrets-sweep.sh [ROOT]
#   ROOT defaults to the enclosing git work tree, else the current directory.
# Exit 0: clean. Exit 1: possible secret found, or a real .env file present.
set -euo pipefail

ROOT="${1:-}"
if [ -z "$ROOT" ]; then
  ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi
cd "$ROOT"

# ---------------------------------------------------------------------------
# 1. Real .env files may never be committed.
#    (.env.example / .env.sample / .env.template / .env.dist are templates.)
# ---------------------------------------------------------------------------
ENV_TEMPLATE_RE='\.env\.(example|sample|template|dist)$'
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git ls-files \
      | grep -E '(^|/)\.env($|\.)' \
      | grep -vE "$ENV_TEMPLATE_RE" \
      | grep -q .; then
    echo "ERROR: real .env file tracked by git" >&2
    exit 1
  fi
else
  if find . \( -path './.git' -o -path '*/node_modules' \) -prune -o \
      \( -name '.env' -o -name '.env.*' \) -print 2>/dev/null \
      | grep -vE "$ENV_TEMPLATE_RE" \
      | grep -q .; then
    echo "ERROR: real .env file present under $ROOT" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. Secret-shaped content.
#
#    High-signal token shapes (kept tight on purpose):
#      AWS access key IDs, GitHub tokens (classic + fine-grained PAT),
#      Slack tokens, Stripe/OpenAI/Anthropic live keys, private key blocks,
#      aws_secret_access_key references.
#    Generic quoted assignments (api_key/secret/token/password/passwd = "...")
#    need a minimum 8-char value so token *generators* (e.g.
#    `token = 'uag_' + randomBytes(...)`) don't trip it; \b keeps
#    `mysecret` / `tokens` out.
#    Lines carrying obvious placeholder markers (example keys, changeme,
#    <angle-bracket> templates, ${VAR}, test-key-not-real, ...) are not
#    secrets and are filtered out.
# ---------------------------------------------------------------------------
SECRET_RE='BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|ghu_[A-Za-z0-9]{36}|ghs_[A-Za-z0-9]{36}|ghr_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|xox[abpse]-[A-Za-z0-9-]{10,}|sk-live-[A-Za-z0-9]{16,}|sk-proj-[A-Za-z0-9]{16,}|sk-ant-[A-Za-z0-9-]{16,}|aws_secret_access_key'
ASSIGN_RE="\b(api_key|secret|token|password|passwd)\b[[:space:]]*=[[:space:]]*[\"'][^\"']{8,}[\"']"
PLACEHOLDER_RE='EXAMPLE|NOT[-_ ]REAL|PLACEHOLDER|CHANGEME|DUMMY|FAKE|X{3,}|YOUR[-_ ]|<[^>]+>|\$\{|test-key'

# Paths never scanned: markdown/docs (examples live there), the CI workflow
# dir and this script itself (pattern text self-matches otherwise).
EXCLUDE_RE='\.md$|(^|/)docs/|(^\.|/)\.github/|secrets-sweep\.sh$'

list_files() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files
  else
    find . \( -path './.git' -o -path '*/node_modules' -o -path '*/dist' \
         -o -path '*/__pycache__' -o -path '*/.pytest_cache' -o -path '*/.venv' \) -prune \
         -o -type f -print 2>/dev/null | sed 's|^\./||'
  fi
}

hits="$(list_files \
  | grep -vE "$EXCLUDE_RE" \
  | tr '\n' '\0' \
  | xargs -0 -r grep -nEI -e "$SECRET_RE" -e "$ASSIGN_RE" 2>/dev/null \
  | grep -viE "$PLACEHOLDER_RE" || true)"

if [ -n "$hits" ]; then
  echo "ERROR: possible secret found in tracked files:" >&2
  printf '%s\n' "$hits" >&2
  exit 1
fi

echo "secrets sweep clean"
