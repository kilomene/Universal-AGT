# Universal AGT — Configuration variable reference (canonical)

One name per concept. This document is the single source of truth for every
configuration variable in the system (W3, 2026-10-05). If a name appears
anywhere else (code, scripts, docs, `.env.example`) with a different spelling
for the same concept, that is a bug — fix it to the name listed here.

## Naming rules

| Prefix | Meaning | Where it is set |
|---|---|---|
| `UAHT_*` | **Operator-facing inputs.** Names the operator types: control-plane server env, host-installer inputs, SDK/CLI env. | shell / `.env` / installer invocation |
| `WORKER_*` | **Host worker runtime config.** Keys in `/opt/agent-host/config/worker.env` (or `$WORKER_CONFIG`, or `WORKER_*` process env, which overrides the file). | written by `scripts/install-host.sh` |
| *(none)* | **Control-plane server internals** (`DATABASE_URL`, `DATA_ENCRYPTION_KEY`, `PORT`, …) and app-level vars. | server `.env` |

Rules:

1. `UAHT_*` names never appear as keys inside `worker.env` — the installer
   maps them to `WORKER_*` keys (mapping table below).
2. `WORKER_*` names never appear as installer inputs.
3. There is exactly one provisioning-token name: `UAHT_PROVISIONING_TOKEN`.
   The old bare `PROVISIONING_TOKEN` is gone (renamed W3, 2026-10-05).
4. The worker runtime tunnel-token name is `WORKER_TUNNEL_TOKEN`;
   `UAHT_TUNNEL_TOKEN` is the installer input only (renamed W3, 2026-10-05).

## Control-plane API (server env / `.env`)

`control-plane/api/.env.example` is the template. Startup runs
`validateStartupConfig()` (`src/lib/config.ts`) **before** opening the DB
pool or listening: production (default — `NODE_ENV` unset or anything other
than `development`) is strict and refuses to boot on any missing/malformed
required variable, naming it precisely. `NODE_ENV=development` relaxes
**only** `UAHT_PROVISIONING_TOKEN` (agent-registration bootstrap mode) and
says so at boot. There are no insecure fallbacks in any mode. The validator
also rejects malformed optional configuration — an invalid
`TUNNEL_INGRESS_HOSTNAME`, tunnel ingress without Cloudflare tunnel
credentials, a half-configured Cloudflare DNS (token without zone or vice
versa), an invalid `PUBLIC_INGRESS_HOSTNAME` / `PORT`, non-positive rate
limits, and conflicting heartbeat thresholds — and reports non-fatal issues
(DNS credentials with no ingress target, `LOG_LEVEL=debug` in production)
as boot warnings. Secret values are never printed, only variable names.

| Variable | Required in production | Default / notes |
|---|---|---|
| `DATABASE_URL` | **yes** | Postgres connection string (Supabase pooler URI works). No default — the server cannot run without it. |
| `DATA_ENCRYPTION_KEY` | **yes** | 64 hex chars (32 bytes, `openssl rand -hex 32`). AES-256-GCM key for project secrets. **Back it up — changing it invalidates stored secrets.** Format is validated at startup. |
| `UAHT_PROVISIONING_TOKEN` | **yes** | The provisioning token. **One token, two uses:** `POST /v1/hosts/register` accepts it as `Authorization: Bearer <token>` (hands-off first-boot host provisioning), and `POST /v1/agents/register` accepts it as the `X-Provisioning-Token` header (constant-time compare). Unset in development = bootstrap mode: only the very first agent registration is open, then 403. Alternative host-registration path: an agent token with the `deploy` permission. |
| `PORT` | no | `3000` |
| `NODE_ENV` | no | `production` behavior is the default; set `development` explicitly to relax the provisioning-token check only. |
| `ARTIFACT_DIR` | no | `./data/artifacts` |
| `LOG_DIR` | no | `./data/logs` |
| `DASHBOARD_DIR` | no | `<repo>/dashboard` (served at `/`) |
| `ARTIFACT_MAX_BYTES` | no | `524288000` (500 MB) upload wire cap |
| `RATE_LIMIT_AGENT_PER_MIN` | no | `120` |
| `RATE_LIMIT_HOST_PER_MIN` | no | `600` |
| `RATE_LIMIT_UNAUTH_PER_MIN` | no | `10` per IP (registration/login-shaped endpoints) |
| `UAHT_ROTATE_RATE_PER_MIN` | no | `10` per credential (rotation endpoints) |
| `HEARTBEAT_SWEEP_INTERVAL_S` | no | `30` |
| `HEARTBEAT_DEGRADED_AFTER_S` | no | `90` |
| `HEARTBEAT_OFFLINE_AFTER_S` | no | `300` |
| `PG_POOL_MAX` | no | `10` |
| `LOG_LEVEL` | no | `info` |
| `CLOUDFLARE_API_TOKEN` | no | Optional; with `CLOUDFLARE_ZONE_ID` enables `POST /v1/domains` DNS management (scoped to Zone / DNS / Edit). |
| `CLOUDFLARE_API_BASE` | no | Test hook only — leave unset in production. Defaults to `https://api.cloudflare.com/client/v4`. |
| `CLOUDFLARE_ZONE_ID` | no | Optional; see above. |
| `CLOUDFLARE_TUNNEL_API_TOKEN` | no | Optional; dedicated token for tunnel config sync (needs Account / Cloudflare Tunnel / Edit). Falls back to `CLOUDFLARE_API_TOKEN` when unset. See `docs/cloudflare.md`. |
| `CLOUDFLARE_ACCOUNT_ID` | no | Optional; required alongside a tunnel token for tunnel route management (tunnel id is parsed from `TUNNEL_INGRESS_HOSTNAME`). |
| `PUBLIC_INGRESS_HOSTNAME` | no | Optional direct-mode ingress target for domain CNAMEs. |
| `TUNNEL_INGRESS_HOSTNAME` | no | Optional; when set, new domains default to tunnel mode (`<tunnel-id>.cfargotunnel.com`). |

## Host installer inputs → worker.env mapping

`scripts/install-host.sh` accepts these `UAHT_*` operator inputs and writes
them into `/opt/agent-host/config/worker.env` (mode `0600`, `agenthost`-owned)
as the `WORKER_*` runtime keys on the right:

| Installer input (`UAHT_*`) | worker.env runtime key (`WORKER_*`) |
|---|---|
| `UAHT_CONTROL_PLANE_URL` (required) | `WORKER_CONTROL_PLANE_URL` |
| `UAHT_HOST_NAME` (required) | `WORKER_HOST_NAME` |
| `UAHT_HOST_TOKEN` + `UAHT_HOST_ID` (pre-provisioned pair, or auto-provisioned via `POST /v1/hosts/register`) | `WORKER_HOST_TOKEN`, `WORKER_HOST_ID` |
| `UAHT_WORKER_VERSION` (default `0.1.0`) | `WORKER_WORKER_VERSION` |
| `UAHT_INGRESS_ENABLED` (`1` to enable) | `WORKER_INGRESS_ENABLED` |
| `UAHT_TUNNEL_TOKEN` (required when ingress enabled) | `WORKER_TUNNEL_TOKEN` |
| `UAHT_REPO_DIR` | — (installer-local: where the Universal-AGT checkout lives) |
| `UAHT_SKIP_APT` | — (installer-local: `1` skips apt installs) |

## Worker runtime (`WORKER_*`)

Read by `host-worker/agent/config.py` from `worker.env`, then `WORKER_*`
process env (overrides the file), then explicit kwargs. Full key list —
see `host-worker/.env.example`:

`WORKER_CONTROL_PLANE_URL`, `WORKER_HOST_NAME`, `WORKER_HOST_TOKEN`,
`WORKER_HOST_ID`, `WORKER_POLL_WAIT` (1–30s), `WORKER_HEARTBEAT_INTERVAL`
(≥5s), `WORKER_WORK_DIR`, `WORKER_APPS_DIR`, `WORKER_WORKER_VERSION`,
`WORKER_CAPABILITIES`, `WORKER_CRASH_LOOP_THRESHOLD`,
`WORKER_CRASH_LOOP_WINDOW_S`, `WORKER_INGRESS_ENABLED`,
`WORKER_INGRESS_PROVIDER`, `WORKER_TUNNEL_TOKEN`.
`WORKER_CONFIG` (env only) overrides the config-file path.

## SDK / CLI

| Variable | Used by | Purpose |
|---|---|---|
| `UAHT_BASE_URL` | `agent-host` CLI, both SDKs | Control-plane base URL (or `--base-url`) |
| `UAHT_API_KEY` | `agent-host` CLI, both SDKs | Agent API key (or `--api-key`) |

## History

- **2026-10-05 (W3):** `PROVISIONING_TOKEN` → `UAHT_PROVISIONING_TOKEN`.
  The auth middleware read the bare name while agent registration and the
  boot check read the prefixed name — one operator setting left the other
  gate on a different token. Now a single token gates both agent and host
  registration.
- **2026-10-05 (W3):** `UAHT_TUNNEL_TOKEN` → `WORKER_TUNNEL_TOKEN` in worker
  runtime (`worker.env`, `WORKER_*` env). The installer input keeps the
  `UAHT_` name; `install-host.sh` maps it into `worker.env` as
  `WORKER_TUNNEL_TOKEN`. The worker no longer reads the `UAHT_` name.
- Earlier: `UAHT_API_URL` → `UAHT_BASE_URL` (CLI/SDK base URL).
