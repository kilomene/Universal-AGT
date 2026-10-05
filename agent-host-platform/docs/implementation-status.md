# Universal-AGT — Implementation Status

**Audit date:** 2026-10-05
**Method:** full read of actual implementation (not READMEs) across control-plane API + database, host worker, agent SDKs, CLI, dashboard, protocol, docs, examples, scripts. Three independent code auditors + baseline test run.
**Baseline test results (all green):** control-plane API 77/77 (vitest) · host worker 116/116 (pytest) · JS SDK 16/16 (node:test) · Python SDK 18/18 (pytest) · CLI smoke 12/12.

> Caveat: the API and worker suites test pure functions and logic with fakes. **Zero HTTP route tests exist** — no test boots the Express app. Every finding marked "broken" or "security hole" below sits in untested code.

**State legend:** ✅ implemented & working · ⚠️ partial / degraded · ❌ broken or missing · 🔒 security finding · 🧪 untested in code path.

---

## Status matrix

| Feature | State | Files involved | What works | What is incomplete / must change | Test status |
|---|---|---|---|---|---|
| Agent registration | ⚠️🔒 | `control-plane/api/src/routes/agents.ts` | Issues `uag_` keys, SHA-256 stored, shown once. `agent.connected` event. | **OPEN endpoint accepts caller-chosen permissions** — anyone can mint a god-mode agent. Unthrottled. No approval workflow. Must gate behind provisioning token or bootstrap-only mode. | security 3 ✅ (route: 401/403/bootstrap) |
| Agent authentication | ✅ | `middleware/auth.ts`, `lib/tokens.ts` | Constant-time verify, kind isolation (agent vs host), suspended→403, `?api_key=` SSE fallback. | `?api_key=` accepted on **all** routes (docs claim stream-only). No replay protection, no rotation endpoint. | tokens 9 ✅ + security 4 ✅ (stream-only scoping, rotation) |
| Agent permissions | ⚠️🔒 | `middleware/auth.ts` (free-form JSONB map), routes | `deploy`, `read_status`, `restart`, `stop`, `approve_deployments`, `manage_secrets`, `manage_domains` enforced on convenience routes. | **Task-type bypass:** non-deploy task types (stop/remove/…) need only `read_status` via `POST /v1/tasks`. **Cancel needs only `read_status`.** No permission registry/enum. No per-resource ACL (global permissions). | ❌ untested |
| Host registration | ⚠️🔒 | `routes/hosts.ts`, `scripts/install-host.sh` | Issues `uagh_` tokens, SHA-256 stored. `host.registered` event. | Open to any agent with `deploy` (inherits registration weakness). Re-running installer registers a *second* host. | ❌ untested |
| Host authentication | ✅ | `middleware/auth.ts`, `routes/worker.ts` | Host token must match `:id`/`host_id` in claim/heartbeat/progress. | — | ❌ untested |
| Host heartbeat | ⚠️ | `routes/worker.ts:54`, `host-worker/health/collector.py` | Reports cpu/ram/disk %, docker status, running apps, worker version every 30s. Sets `online` + `last_seen`. | **No stale-host sweeper** — nothing ever marks degraded/offline. Heartbeat **clobbers** operator-set `draining`/`degraded`. Metrics from local state, not Docker (can lie). No per-container stats, no uptime. `docker version` subprocess has no tight timeout. | ❌ untested |
| Task creation | ✅ | `routes/tasks.ts`, `lib/stateMachine.ts` | `queued` / `awaiting_approval` (manual), cursor pagination, idempotency keys. | Task idempotency race → 500 on concurrent duplicates (no 23505 handling). | state machine 37 ✅ (pure) |
| Task claiming (atomic) | ✅ | `routes/worker.ts` `tryClaim` | Single `UPDATE … FOR UPDATE SKIP LOCKED` — exactly one host wins. Long-poll `?wait=N`. | — | worker claim builder 25 ✅ (no live HTTP) |
| Task execution | ✅ | `host-worker/executor/dispatcher.py`, `handlers.py` | 17-type allowlist, fixed handlers, argv-only subprocess (AST-enforced no `shell=True`). | 16 of 17 handlers untested. `docker-run` `extra_args` = **arbitrary docker flags → host root** (🔒 critical). | policy 19 ✅, pipeline 12 ✅ (fake docker) + security 4 ✅ (extra_args rejection) |
| Task retry | ❌ | `lib/stateMachine.ts`, `routes/worker.ts` | `attempts` incremented on claim. | **No retry classification** (safe/conditional/non-retryable). `max_attempts` never read. `retrying` state unreachable. Failed task can be claimed forever. **No claim lease** — dead worker's task stuck in `claimed`/`running` forever, no sweeper. | ❌ untested |
| Task cancellation | ⚠️🔒 | `routes/tasks.ts:211` | Transitions to `cancelled`, emits event. | Needs only `read_status` (should require ownership or `deploy`). Hosts cannot report cancelled. | ❌ untested |
| Task idempotency | ⚠️ | `lib/idempotency.ts`, `routes/tasks.ts`, `routes/deployments.ts` | Canonical-JSON compare, replay (200 + `idempotent_replay:true`), 409 on conflict, deployments transactional. | **Deployment replay broken** for artifact/manual deploys: reads `mode`/`artifact_id` from row that lacks those columns → false 409s. Task POST has 500 race. | idempotency 14 ✅ (pure fn only) |
| Artifact upload | ⚠️🔒 | `routes/artifacts.ts` | Two-phase init + PUT octet-stream, SHA-256 verified after write, path-traversal guard. | **No size cap on the wire** — full body written before verification (disk-fill DoS). | security 3 ✅ (413 upfront/stream-chunk/init) |
| Artifact download | ⚠️🔒 | `routes/artifacts.ts` download | Streams to `.part`, atomic replace, size check. | **Any host token can download any artifact** — no scoping. Worker: no per-chunk read timeout (slow-drip hold). | security 3 ✅ (403 scoping) |
| Artifact verification | ✅ | `host-worker/deployments/pipeline.py` | Requires `sha256:<hex>`, `hmac.compare_digest`, quarantine on mismatch **before any docker call** (zero-docker-calls verified). | Quarantine unbounded. | ✅ (fake docker) |
| Deployment (auto) | ✅ | `routes/deployments.ts`, `host-worker/deployments/pipeline.py` | Full pipeline: download→verify→extract→manifest→resource check→build→run→health→register. Blue/green-ish: old container kept until new passes health. | Same-version redeploy deletes old container before health (downtime, no rollback). Old `superseded` containers/images never GC'd. **Compose rollback broken** (tears down nothing, `docker.start(None)` → error). | pipeline 12 ✅ (fake docker) |
| Deployment (manual) | ✅ | `routes/tasks.ts` approve/reject | `awaiting_approval` → approve→`queued`, reject→`cancelled`. `approve_deployments` perm. Audit: `task.approved`/`rejected` with actor id. | No notification path (poll `?status=awaiting_approval`). CLI has no approve/reject. Dashboard has no approvals UI. Approval inherits open-registration weakness. | ❌ untested |
| Docker execution | ✅ | `host-worker/docker/client.py` | Real CLI wrapper, argv lists, container names `uaht-{project}-{version}`, `--memory`/`--cpus`, restart policies allowlisted. | Image tags unsanitized (any registry pullable). `handle_docker_run` passes `restart` unvalidated. Published ports bind `0.0.0.0`. | ❌ untested (fake only) |
| Health checks | ⚠️ | `host-worker/health/checker.py` | Real HTTP checks, `GET http://127.0.0.1:{port}{path}`, exact-200, 2s poll, configurable timeout (default 120s). | **HTTP only** — no TCP/process/container checks. Success = exactly 200 (not 2xx). Compose port discovery regex-brittle. | ❌ untested |
| Rollback | ❌🔒 | `routes/deployments.ts:293`, `host-worker/deployments/pipeline.py` | Worker-side auto-rollback on health failure works (fake-docker tested): new removed, previous restarted. | **API rollback endpoint ALWAYS 500s** — INSERTs duplicate `(project_id, host_id, version)` violating unique constraint. Success path unreachable. No test covers it. | pipeline ✅ (worker side) / API ❌ |
| Logs | ✅ | `host-worker/logs/store.py`, `executor/dispatcher.py` | Per-task + per-deployment logs, 10MiB × 3 rotation, `SecretScrubber` (token + payload secrets ≥4 chars) on disk and control-plane sends. | Secrets < 4 chars not scrubbed. Task-id unsanitized in log filename (path traversal if task id malicious). No API log-query endpoint (only `logs` task type). | security 2 ✅ (sanitize_log_id) |
| Events | ✅ | `database/schema.sql` trigger, `lib/events.ts`, `routes/events.ts` | Append-only trigger blocks UPDATE/DELETE. Rich emission (26 event types). SSE stream with pg LISTEN/NOTIFY + 5s backstop + keepalive. | TRUNCATE not blocked. Stream has `?limit=` but no `since` replay. | 005 migration ✅ (event trigger; needs superuser) |
| Secrets | ⚠️ | `lib/secrets.ts`, `routes/secrets.ts`, `routes/projects.ts` | AES-256-GCM, key validated, names-only reads, plaintext never logged/returned. | **`getDecryptedProjectSecrets` is dead code — no endpoint delivers secrets to hosts.** Secret injection into deployments unimplemented (write-only). | crypto 9 ✅ + security 3 ✅ (scoped worker pull, never persisted) |
| Resource limits | ⚠️ | `deployments/manifest.py`, `docker/client.py` | Manifest validates `memory` regex + positive `cpu`; `--memory`/`--cpus` passed to docker. Pre-deploy resource *check* exists in pipeline. | `environment-update` drops memory/cpus on recreate. No aggregate capacity accounting per host. No port registry. | manifest 60 ✅ |
| Port management | ❌ | `deployments.ports` (jsonb, write-never) | — | **No registry, no allocation, no collision detection** (DB/OS/Docker). Two deployments can claim the same port silently. | ❌ |
| Host recovery | ❌ | `host-worker/agent/main.py` | Docker `unless-stopped` restart policy brings containers back after reboot. | **No reconciliation**: desired (state.json) vs actual Docker never compared. Missing/rm'd container stays `running` in state — control plane sees a lie. No interrupted-deploy resumption. Corrupt `state.json` → bare `json.load` crash poisons heartbeats. | ❌ |
| Worker recovery | ⚠️ | `agent/agent-host-worker.service`, `main.py` | systemd `Restart=always`, SIGTERM drain, fixed 5s claim retry, heartbeat `failures` counter. | **No exponential backoff** (fixed 5s forever, no jitter). `failures` counter write-only. No post-restart version health gate. | ❌ |
| Worker updates | ⚠️ | `host-worker/updater/self_update.py` | Real: download → SHA-256 verify → extract → byte-compile check → atomic symlink swing → restart → rollback on restart-cmd failure. | **No signature verification** (trusts control-plane SHA only). **No post-restart health check** — bad update + `StartLimitBurst` = bricked host. Tar symlink escape. Update advertised every 30s (noisy). | security 9 ✅ (data_filter, health gate, --self-check) |
| Dashboard | ⚠️ | `dashboard/index.html`, `js/app.js` | Hosts/apps/deployments/tasks panels, live SSE events, 10s polling, sessionStorage-only key, 401→re-auth. Working read-only monitor. | **No actions at all** (no approve/reject/restart/stop). No agents/projects/secrets/approvals pages. `awaiting_approval` shown but not actionable. | ❌ |
| CLI | ✅ | `cli/src/agent_host_cli/main.py` | 12 commands, global `--json`, env/flag config, `deploy --artifact` uploads. | Missing: `agents`, `projects`, `deployments` list, **approve/reject**, `secrets`, task `cancel`. Smoke test omits `domains`. | smoke 12/12 ✅ |
| SDK (JS) | ✅ | `agent-sdk/javascript/src/client.js` | 34 methods, zero-dep, SSE async generator, `deploy()` helper with polling. Full protocol parity. | **`streamEvents` drops `since`** (accepted, never sent) — real bug, untested. No `getLogs` convenience. `initArtifact` Windows-hostile path split. | 16/16 ✅ |
| SDK (Python) | ✅ | `agent-sdk/python/src/uaht_sdk/` | Method-for-method parity with JS, `since` forwarded, `on_event` callback. | Same missing `getLogs`. `deploy()` assumes dict for projects list. | 18/18 ✅ |
| Cloudflare | ✅ | `lib/cloudflare.ts`, `routes/domains.ts` | Idempotent CNAME ensure/delete, proxied, TTL 300, graceful `dns_pending` when unconfigured, hostname regex, scoped token. | Same-hostname on multiple deployments not prevented. Non-CNAME name collision → error entry (handled). **No ingress story**: DNS alone can't reach an outbound-only host (Phase 7). | 8 ✅ (mocked fetch) |
| Public ingress | ❌ | — | — | Nothing. Architectural gap: outbound-only host cannot receive public traffic without a tunnel/relay. Phase 7 must design honestly. | — |
| Muse integration | ⚠️ | SDK + `docs/agent-guide.md` | Everything Muse needs exists via SDK/API: register, project, artifact, deploy, status, logs (task), services, rollback. | No `docs/agent-integration.md` (AI-optimized). No `getLogs` convenience. Open registration undermines auth. | — |
| Instinct integration | ⚠️ | (same as Muse) | Protocol is agent-neutral — Instinct is just another agent row. | Same gaps as Muse. No Instinct-specific work needed by design. | — |
| Security posture | 🔒 | (see Security audit below) | No SQL injection (all parameterized). No `shell=True` (AST-enforced). Secrets encrypted + redacted. Append-only events. | Critical/high findings listed below. | AST static ✅ |
| CI | ✅ | `.github/workflows/ci.yml` | 6 jobs green, concurrency cancel-in-progress, secrets sweep. | Route/integration tests absent from CI (can't run without Postgres — needs service container). | ✅ green |

---

## Security audit findings (ranked)

1. **CRITICAL — `docker-run` `extra_args` argv injection → host root.** `executor/handlers.py:312` appends caller-supplied flags verbatim to `docker run`. Payload `["--privileged","-v","/:/host","--pid=host"]` = full host compromise. The docker-group membership makes this host-root directly. **Fix: remove `extra_args` or restrict to an allowlisted flag set.**
2. **HIGH — Open agent registration with self-granted permissions** (`routes/agents.ts`). Collapses permission model, approval gate, provisioning requirement. **Fix: gate behind `UAHT_PROVISIONING_TOKEN` or bootstrap-only mode.**
3. **HIGH — Task-type permission bypass** (`routes/tasks.ts:34`): non-deploy task types need only `read_status` → read-only agent can queue `stop`/`remove`. **Fix: permission map per task type.**
4. **MEDIUM — API rollback endpoint always 500** (`routes/deployments.ts:293`): unique-violation on `(project_id,host_id,version)` re-insert. **Fix: rollback = new deployment row with new version pointer, or drop the unique constraint interaction.**
5. **MEDIUM — Deployment idempotency replay broken** for artifact/manual deploys (`deployments.ts:94` reads `mode`/`artifact_id` off a row lacking those columns → false 409s). **Fix: store mode+artifact on the deployment row or derive from the linked task.**
6. **MEDIUM — Any host can download any artifact** (`routes/artifacts.ts`): no scoping. **Fix: scope to host's assigned tasks/deployments.**
7. **MEDIUM — Task cancel on `read_status`** (`routes/tasks.ts:211`). **Fix: require `deploy` or ownership.**
8. **MEDIUM — Artifact upload has no wire size cap** (disk-fill before 422). **Fix: enforce Content-Length / stream cap.**
9. **MEDIUM — Tar symlink escape** in `pipeline.extract_archive` and `updater/self_update.py` (check-before-extract TOCTOU). **Fix: `tarfile.data_filter` (py3.12) or post-create resolution.**
10. **MEDIUM — `build.dockerfile`/`build.context` path traversal** outside artifact dir (`Path(extract_dir)/dockerfile` with `../`). **Fix: confine to extract dir.**
11. **MEDIUM — `artifact-upload` can exfiltrate `config/worker.env`** (destination confined to work_dir, but worker.env lives under it). **Fix: sensitive-path denylist.**
12. **MEDIUM — Self-update trusts control-plane SHA only, no post-restart gate** → bad update bricks host. **Fix: post-restart health verification + signature or pinned-key verification.**
13. **LOW — Secrets delivery to hosts unimplemented** (`getDecryptedProjectSecrets` dead). Secrets are write-only. **Fix: worker-authenticated secrets endpoint + injection at deploy time.**
14. **LOW — Heartbeat clobbers `draining`/`degraded`.** **Fix: heartbeat must not overwrite operator-set states.**
15. **LOW — No stale-host sweeper / no claim lease.** Dead hosts and stuck tasks persist forever. **Fix: configurable thresholds, `host.offline` sweeper, claim lease + requeue.**
16. **LOW — Query-string `?api_key=` accepted globally.** **Fix: restrict to `/v1/events/stream`.**
17. **LOW — Registration endpoints unthrottled.** **Fix: apply rate limiter to unauthenticated routes.**
18. **LOW — No token rotation / replay protection / admin suspend endpoints.** **Fix: rotation + admin routes.**

---

## Phase 6 resolution (2026-10-05)

Findings 1–3, 6–18 from the audit above were the Phase 6 scope (findings
4–5, the rollback-500 and deployment-idempotency fixes, were already
resolved in Phase 3). Status after Phase 6:

| # | Finding | Resolution |
|---|---|---|
| 1 | CRITICAL `extra_args` → host root | **Fixed.** Field removed from the protocol; `policy.reject_disallowed_fields` raises `TaskRejected` (task failed, nothing executed); `DockerClient.run()` lost the parameter. Test: dispatcher rejects, zero docker calls. |
| 2 | HIGH open agent registration | **Fixed.** `X-Provisioning-Token: <UAHT_PROVISIONING_TOKEN>` required (constant-time); bootstrap-only first registration when unset; server refuses to boot in production without the token. Unauthenticated bucket: 10/min per IP. |
| 3 | HIGH task-type permission bypass | **Already fixed in Phase 3 — verified.** `permissionForTaskType` covers all 17 types (test asserts the full map); unknown types default closed to `deploy`; cancel needs `deploy` or creatorship (route test: 403 for non-owner). |
| 6 | MEDIUM any host downloads any artifact | **Fixed.** Scoped to artifacts referenced by tasks the host claimed (`payload.artifact_id`); 403 otherwise. Agent `read_status` path unchanged. |
| 7 | MEDIUM cancel on `read_status` | **Fixed** (was already tightened; now route-tested: 403 for non-owner without `deploy`). |
| 8 | MEDIUM artifact upload no wire cap | **Fixed.** `ARTIFACT_MAX_BYTES` (default 500MB) enforced on `Content-Length` upfront, per-chunk while streaming, and on declared `size` at init → 413 `payload_too_large`. |
| 9 | MEDIUM tar symlink escape | **Fixed.** `tarfile.data_filter` in both `pipeline.extract_archive` and `updater/self_update.py`. Test: symlink + file-through-symlink tar blocked, nothing written outside. |
| 10 | MEDIUM dockerfile/context traversal | **Fixed.** `pipeline.confined_under` confines `build.dockerfile`, `build.context`, and payload `compose_file` to the extracted artifact tree. `handlers._confined_path` verified + tested. |
| 11 | MEDIUM `artifact-upload` exfiltrates `worker.env` | **Fixed.** Sensitive-path denylist: refuses anything at/under `<work_dir>/config/` or the configured `worker.env` path. |
| 12 | MEDIUM self-update: SHA-only trust, no post-restart gate | **Partially fixed, honestly documented.** Post-restart health gate implemented (boot marker + active unit within `WORKER_UPDATE_HEALTH_TIMEOUT_S`, rollback on failure; `--self-check` CLI). Code-signing with an offline key NOT implemented — documented as an explicit trust-model limitation (control plane is the trusted orchestrator). |
| 13 | LOW secrets delivery unimplemented | **Fixed.** `GET /v1/worker/projects/:id/secrets` (host token, scoped to live work); worker pulls at deploy time, injects as container env, never persists to `state.json` (tested). |
| 14 | LOW heartbeat clobbers draining | **Already fixed in Phase 2 — verified** by route test (draining survives heartbeat). |
| 15 | LOW no sweeper / claim lease | **Already fixed in Phase 2/3** (not in Phase 6 scope). |
| 16 | LOW `?api_key=` global | **Fixed.** Honored only on `GET /v1/events/stream`; all other endpoints require the header (route-tested both directions). |
| 17 | LOW registration unthrottled | **Fixed** via the unauthenticated bucket (finding 2). |
| 18 | LOW no rotation / replay protection | **Rotation fixed** (`/me/rotate`, `/hosts/:id/rotate-token`, tested). **Replay protection: accepted limitation**, documented in `docs/security.md` and the PROTOCOL changelog — no HMAC scheme invented mid-phase. |
| — | TRUNCATE on events | **Fixed.** Migration `005_events_truncate_block.sql` installs an event trigger aborting `TRUNCATE` on `events` (row triggers can't block it; needs superuser — documented in the migration). |

Security test totals after Phase 6: worker 216 pytest (28 new), API 157+1 vitest (32 new), JS SDK 24, Python SDK 24, CLI smoke 20/20 — all green.

---

## Phase 7 resolution (2026-10-05)

Scope: honest ingress design — DNS≠reachability; optional modular ingress
(worker-managed `cloudflared` tunnel), provider-neutral interface.

| What | Status |
|---|---|
| Ingress abstraction (`host-worker/ingress/`) | ✅ `IngressProvider` interface (`name`, `setup`, `add_route`, `remove_route`, `sync_routes`, `status`, `shutdown`); loopback-only targets enforced; route-table builder + `config.yml` renderer (hostname re-validated, YAML-injection safe). |
| `cloudflare-tunnel` provider | ✅ Supervised `cloudflared tunnel --token <TOKEN> run` (outbound-only); pinned release 2026.10.0 with SHA-256 verification (official per-asset digests, amd64 re-verified by download); crash restart with backoff; argv-only subprocess; token never logged. Download failure / missing token → clear log, ingress stays disabled, worker keeps running. |
| `ingress-sync` task type (18th) | ✅ Handler + policy allowlist + `deploy` permission + `safe` retry class; empty payload, pulls `GET /v1/worker/domains`; best-effort in-process sync after successful deploy/remove; PROTOCOL §3.2/§5 + Changelog updated. |
| Control plane tunnel mode | ✅ `POST /v1/domains` accepts `ingress: 'tunnel'\|'direct'` (default: tunnel when `TUNNEL_INGRESS_HOSTNAME` set, else direct); CNAME `→ <tunnel-id>.cfargotunnel.com` in tunnel mode; 422 when tunnel requested without the hostname configured; tunnel-mode changes queue `ingress-sync` for the host. |
| Docs | ✅ `docs/cloudflare.md` rewritten around the three honest modes; `docs/host-install.md` + installer + `.env.example` files cover the new vars; PROTOCOL changelog. |
| Tests | ✅ worker 244 pytest (28 new ingress), API 166 vitest (8 new), JS SDK 24, Python SDK 24, CLI smoke 20/20 — all green. |
| Accepted limitations (documented, not fixed) | For dashboard-created (token) tunnels, Cloudflare reads public-hostname routes from the tunnel's dashboard config, not from the worker's `config.yml` — the operator must add each hostname in **Zero Trust → Networks → Tunnels → Public hostnames**. Tunnel creation, token provisioning, and the dashboard hostname entries are human steps by design. The tunnel token is visible in the host's process table to local root (argv) — accepted, documented. |

---

## Acceptance criteria mapping (spec §59, 38 items)

| # | Criterion | Verdict |
|---|---|---|
| 1 | Muse can authenticate | ✅ (needs registration gate fix for production) |
| 2 | Instinct can authenticate | ✅ (same) |
| 3 | Persistent host can authenticate | ✅ |
| 4 | Host requires no publicly exposed IP | ✅ by design (outbound-only) |
| 5 | Host connects outbound | ✅ |
| 6 | Host survives reboot | ⚠️ containers return via restart policy; **no reconciliation** — needs live test |
| 7 | Worker survives crash | ✅ systemd `Restart=always` — needs live test |
| 8 | Tasks persist | ✅ (DB-backed) |
| 9 | Tasks are idempotent | ⚠️ task keys yes; **deployment replay broken** for artifact/manual |
| 10 | Tasks are atomically claimed | ✅ (`FOR UPDATE SKIP LOCKED`) |
| 11 | Artifacts upload | ✅ |
| 12 | Artifacts verify | ✅ (SHA-256, quarantine) |
| 13 | Automatic deployment works | ✅ (fake-docker proven; needs live test) |
| 14 | Manual deployment works | ✅ (state machine; needs live test) |
| 15 | Docker deployment works | ⚠️ (fake-docker only; no daemon here) |
| 16 | Multiple applications work | ⚠️ (isolation design sound; no port registry — collision possible) |
| 17 | Health checks work | ⚠️ (HTTP-only) |
| 18 | Failed deployment handled | ✅ (fake-docker proven) |
| 19 | Rollback works | ⚠️ worker-side ✅; **API endpoint ❌ always 500** |
| 20 | Logs work | ✅ |
| 21 | Events work | ✅ |
| 22 | Secrets protected | ⚠️ encrypted at rest; **delivery to hosts unimplemented** |
| 23 | Resource limits work | ⚠️ manifest→docker flags; no aggregate accounting |
| 24 | Agent disappearance doesn't stop deployment | ✅ by design (control plane is source of truth) |
| 25 | Host disappearance detected | ❌ no sweeper |
| 26 | Host reconnects | ⚠️ fixed 5s retry, no backoff |
| 27 | Worker updates safely | ⚠️ no post-restart gate |
| 28 | Dashboard works | ⚠️ read-only |
| 29 | CLI works | ✅ |
| 30 | SDK works | ✅ (minus JS `since` bug) |
| 31 | Muse integration works | ⚠️ (no agent-integration.md yet) |
| 32 | Instinct integration works | ⚠️ (same) |
| 33 | Cloudflare works where configured | ✅ (mocked; needs live CF test) |
| 34 | Public ingress works | ❌ **not designed** — Phase 7 |
| 35 | Domain routing works | ⚠️ DNS only; no traffic path |
| 36 | Security tests pass | ✅ Phase 6 findings resolved; worker 216, API 158, JS 24, Python 24, CLI smoke 20/20 |
| 37 | End-to-end tests pass | ❌ no E2E suite yet |
| 38 | Documentation accurate | ⚠️ mostly; `agent-integration.md` missing, some drift |

**Score: 15 ✅ · 16 ⚠️ · 7 ❌** (of 38)

---

## What the phases must do (roadmap)

- **Phase 2** — worker reconnect backoff+jitter; stale-host sweeper with configurable thresholds (online/degraded/offline); post-reboot reconciliation (state.json vs `docker ps`); crash-loop detection → `CRASH_LOOP` event; systemd hardening (`MemoryMax`, `ProtectKernel*`, syscall filter where safe).
- **Phase 3** — retry classification + `max_attempts` enforcement + claim lease/requeue; fix API rollback 500; fix deployment idempotency replay; port registry with DB+OS+Docker collision checks; blue/green same-version redeploy fix.
- **Phase 4** — multi-app isolation verification (fake-docker E2E: 3 apps, ports, fs, env, independent stop); compose rollback fix; GC of superseded containers/images.
- **Phase 5** — `docs/agent-integration.md`; `getLogs` in both SDKs; CLI `approve`/`reject`, `projects`, `agents`, `deployments` list, `secrets`, task `cancel`; JS `since` fix; smoke test covers `domains`.
- **Phase 6** — fix security findings 1–18 in priority order; add security tests (auth bypass, permission bypass, path traversal, tar-slip, extra_args rejection, artifact scoping); document worker privilege rationale.
- **Phase 7** — honest ingress design: document why DNS≠reachability; implement optional modular ingress (cloudflared tunnel managed by worker, or control-plane-relayed); keep provider-neutral interface.
- **Phase 8** — dashboard approvals UI + destructive-action confirmations + agents/projects pages; CLI `--json` already there — verify all commands; SDK parity re-check.
- **Phase 9** — deterministic E2E suite (demo app; agent-disappearance; manual approve/reject; broken app; health-fail rollback; multi-app) against fake docker + real API logic; mark live-infra needs explicitly.
- **Phase 10** — update all docs to match reality; production deployment guide.

## What was NOT rebuilt

The architecture from the original build is preserved: Supabase/PostgreSQL schema, Express API, Python worker, both SDKs, CLI, static dashboard, protocol. This document records gaps to fix in place — no competing design introduced.
