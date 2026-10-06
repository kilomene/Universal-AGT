# Universal-AGT — Implementation Status

**Rewritten 2026-10-06 (WS-J final production hardening pass).** Every claim
below was re-verified against the implementation on this date — routes,
middleware, sweepers, worker handlers, SDKs, CLI, migrations, and the test
suites that were runnable locally. There is exactly one status presentation
in this document: the three categories below. Historical phase notes at the
bottom are narrative only and carry no competing scores.

**2026-10-06 follow-up (WS4):** doc reality re-audit after the
rollback/SSRF/volumes/compose/docker/security follow-up fixes — C1
marked resolved (migration 008), B9/A16/A30 updated, compose lifecycle
parity added (A36), new C11 (compose `environment-update` limitation),
obsolete `001…007` migration claims corrected to `001…010`.

**2026-10-06 follow-up (Final Remaining Fixes pass, WS5 doc sweep):**
agent lifecycle (suspend/resume/revoke, §12), project ownership ACLs
(§10/§11, migration 011), host scheduler (§8/§9), rollback failure
durability (§§4–7, migration 012), claim `assigned_to` fix (§13),
6to4 denied range (§2). Migrations corrected to `001…016`; A3, A8,
A16, A18, A29, A30, C6 updated; new A37–A39; duplicate C2 row removed.

**Last verified evidence:**
- Control-plane API: **400/400** vitest (2026-10-06 final pass: 29→30 files).
- Host worker: **636/636** pytest, 2 skipped (2026-10-06 final pass).
- JS SDK: **44/44** `node:test` (2026-10-06).
- Python SDK: **43/43** pytest locally 2026-10-06 (the earlier 40/41 failure
  was a test-authoring bug in the fixture assertion — fixed, client code was
  always correct).
- CLI smoke: 20/20 invocations (19 commands; from CI).
- CI: **12 jobs green 2026-10-06** (run 37424657757): control-plane,
  migration-validation, js-sdk, host-worker, installer, python-sdk, cli,
  dashboard, security, secrets-sweep, build-consistency, e2e (API against a
  real PostgreSQL 16 service container: migration chain 001–010 applied
  via `npm run migrate`, API booted, claim exclusivity proven — that run
  predates migrations 011/012; re-running the CI e2e job against the
  full 001–016 chain is part of the next CI run, not yet done).
- Dashboard: `check.py` + dashboard↔route contract match run in CI;
  `index.html` parses, `app.js` passes `node --check`. **No functional
  browser test has ever exercised the UI.**

> What the suites cannot prove (no test uses a real Docker daemon, real
> systemd, real Cloudflare, or a real Supabase project): those items live
> in Category B, mapped to `docs/e2e-live-checklist.md`.

---

## Category A — IMPLEMENTED AND TESTED

Code exists, tests pass (local suites and/or the 12-job CI). Design
boundaries that are *documented in code* are noted inline — they are not
hidden gaps.

| # | Area | What is implemented | Evidence |
|---|---|---|---|
| A1 | Agent registration gate | `X-Provisioning-Token: <UAHT_PROVISIONING_TOKEN>` required (constant-time compare); bootstrap-only first registration when unset; server refuses to boot in production without the token; unauthenticated bucket 10/min per IP | `security.test.ts` / `securityAuditW16.test.ts` |
| A2 | Agent authentication | SHA-256-hashed bearer keys, agent/host kind isolation, suspended→403, rotation endpoints (`/agents/me/rotate`, `/hosts/:id/rotate-token`); `?api_key=` honored **only** on `GET /v1/events/stream` | `tokens.test.ts` (9), route tests both directions |
| A3 | Permissions | Enforced registry is exactly 8 names (`deploy`, `approve_deployments`, `read_status`, `restart`, `stop`, `manage_secrets`, `manage_domains`, `admin`); per-type map covers all 18 task types (`permissionForTaskType`, unknown types default closed to `deploy`); cancel needs `deploy` or creatorship. `admin` (2026-10-06) is operator-grade: agent suspend/resume/revoke, project owner assignment, membership management — `requireOperator` also accepts the provisioning token bearer | security + route tests asserting the full map |
| A4 | Host registration | `uagh_` tokens via `X-Provisioning-Token` header **or** `Authorization: Bearer <provisioning token>` **or** an agent key with `deploy` (`provisioningOrDeploy`); `host.registered` event | CI e2e smoke + route tests |
| A5 | Host authentication | Host token must match `:id`/`host_id` on claim/heartbeat/progress (`requireHost`); kind isolation both directions | security tests |
| A6 | Heartbeats + stale-host sweeper | Heartbeat posts stats every 30s; sweeper marks `degraded`/`offline` at 90s/300s (`HEARTBEAT_*_S`); heartbeats revive `offline`/`degraded` but never overwrite operator-set `draining` | `hostSweeper.test.ts`, route tests |
| A7 | Task queue + idempotency | Create/poll/cancel, cursor pagination, idempotency keys (tasks: `type`+`payload`; deployments: 6-field compare; 409 on conflict, 200+`idempotent_replay` on replay; 23505 race handled) | `idempotency.test.ts`, `e2eFlows.test.ts` (21) |
| A8 | Atomic claims | `UPDATE … FOR UPDATE SKIP LOCKED` — exactly one host wins; long-poll `?wait=N`; claim stamps `lease_expires_at` (`TASK_CLAIM_LEASE_S`, default 600s), refreshed on every progress report. (2026-10-06 §13: the claim no longer stamps `assigned_to` — that field is the agent-requested host pin from task creation, so requeued tasks after a host death are claimable by other hosts) | CI e2e proves exclusivity against real PG 16 (2nd claim → 204); worker E2E proves exactly-once behavior functionally |
| A9 | Claim leases + retry policy | Stuck-task sweeper (60s) requeues lease-expired work (`task.requeued`, attempt counted) or fails it when `attempts ≥ max_attempts` / type not retry-safe (`deploy`/`restart`/`start`/`stop`/builds only if the attempt never reached `running`; `remove`/`rollback` never auto-retry) | `recoveryLeases`, `taskSweeper` (14), `retryPolicy` (9) tests |
| A10 | Artifact upload | Two-phase init + PUT octet-stream; `ARTIFACT_MAX_BYTES` (500MB) enforced on `Content-Length` upfront, per-chunk while streaming, and on declared `size` → 413 `payload_too_large`; server verifies size + SHA-256 after write | artifact security route tests |
| A11 | Artifact download scoping | Host token may download only artifacts referenced by tasks *that host claimed* (`payload.artifact_id`); 403 otherwise; agent `read_status` downloads unchanged | security tests (403 scoping) |
| A12 | Deploy pipeline | Worker re-verifies SHA-256 (`hmac.compare_digest`) **before any docker call** (zero-docker-calls on mismatch, quarantine); manifest validated against PROTOCOL §4; resource limits → `--memory`/`--cpus` | pipeline tests (12), worker E2E |
| A13 | Manual approval | Task created in `awaiting_approval`; approve→`queued` (+ deployment →`approved`, `deployment.approved` event) / reject→`cancelled`; `approve_deployments` permission; dashboard Approvals panel with dossier + confirmation modals | `e2eFlows` approve/reject semantics, acceptance step 10 |
| A14 | Port registry | Fixed `host_port` reserved in `port_allocations` (unique per host+port → 409); worker bind-tests at OS level and scans `docker ps` published ports before `docker run`/`compose up` | port registry tests |
| A15 | Health checks | HTTP `GET http://127.0.0.1:{port}{path}`, exact-200, 2s poll, configurable timeout (default 120s). **Design boundary:** HTTP only — no TCP/process/container check types | exercised over real HTTP in worker E2E |
| A16 | Rollback | **Unified implementation** (`host-worker/deployments/rollback.py`) used by BOTH paths: automatic (pipeline health-fail) and explicit (`type=rollback` task). Phases: verify-target before any teardown → restore → **real** health check on the restored target (`health_status` recorded honestly, never assumed) → commit. Container targets restore with `docker start`; compose targets with `compose up` from the persisted compose file (also covers targets past the `DEPLOY_KEEP_GENERATIONS=2` GC window via rebuild-from-contract). `POST /v1/deployments/:id/settle-rollback-ports` reconciles port reservations on both paths (settle failure recorded, never fatal); verify-before-destroy preserved on the explicit path. (2026-10-06 §§4–7: same-port explicit rollback fixed — verify tolerates the current deployment's own bind, teardown-before-restore; rollback failures persist `rollback_failed` / `partially_reconciled` durably (never a false success); port registry settled via settle-rollback-ports on both paths; the reconcile pre-pass recovers interrupted rollbacks; `rollback_failed` added to `GCABLE_STATUSES`, the worker ingress `TERMINAL_DEPLOYMENT_STATUSES`, the control-plane `failDomainsForDeployment` triggers, `NON_ROUTABLE_DEPLOYMENT_STATUSES`, and the `deployments.status` CHECK (migration 012)) | `test_rollback_unified.py` (10), `test_handlers_rollback.py` (10), `test_rollback_gc_window.py` (2), `rollbackFailure.test.ts`, `rollbackFailedMigration.test.ts`, acceptance steps 8/9 |
| A17 | Logs | Per-task/deployment logs, 10MiB × 3 rotation, `SecretScrubber` on disk and on control-plane sends. **Documented floor:** values < 4 chars not scrubbed | security tests |
| A18 | Events journal | Append-only (row trigger blocks UPDATE/DELETE; migration `005` = `REVOKE TRUNCATE ON events FROM PUBLIC`, stops non-owner roles, no superuser needed); 49 event types emitted (incl. the 2026-10-06 agent lifecycle trio `agent.suspended`/`agent.resumed`/`agent.revoked`, payloads carry no secrets); SSE with pg LISTEN/NOTIFY + 5s backstop poll + keepalive | 005 migration test, SSE tests |
| A19 | Secrets | AES-256-GCM at rest (`DATA_ENCRYPTION_KEY`, 64 hex); names-only reads; worker pulls decrypted values via host-token-only endpoint scoped to live work; injected as container env; never in `state.json`, logs, or events. **Documented:** payload-carried secrets live in plaintext in the task row — the encrypted store + worker pull is the preferred path | `secretsRoutes` (11), `secretsCrypto` (9), E2E |
| A20 | Rate limits | 120/min per agent key, 600/min per host token, 10/min per IP unauthenticated, 10/min per credential on rotation endpoints | rate-limit tests |
| A21 | Worker hardening | Command allowlist (never a raw shell string from the network); `extra_args` removed from the protocol (rejected by policy, zero docker calls); `tarfile.data_filter` extraction; manifest path confinement; `artifact-upload` sensitive-path denylist (`config/`); no `shell=True` (AST-enforced) | worker security suites, `test_compose_security.py` |
| A22 | Reconcile on boot | Worker compares `<work_dir>/deployments/*/state.json` vs `docker ps` **before** claiming: stopped→`docker start`, missing→rebuild from stored contract; corrupt `state.json` quarantined aside, never fatal. **Documented:** secrets deliberately not re-injected (recreate runs without secret env until next redeploy) | 10 reconcile unit tests |
| A23 | Crash-loop detection | Docker `RestartCount` rising by `WORKER_CRASH_LOOP_THRESHOLD` (5) within `WORKER_CRASH_LOOP_WINDOW_S` (300s) → container stopped, flagged `crash_loop`, `service.crash_loop` emitted; worker never restarts a flagged container | crash-loop tests |
| A24 | Reconnect backoff | Heartbeat/claim failures back off exponentially (5s → 300s cap, ±25% jitter), reset on success; SIGTERM stays responsive | backoff unit tests |
| A25 | Self-update | SHA-256-verified tarball, byte-compile + import check, atomic symlink swing, post-restart health gate (boot marker + active unit within `WORKER_UPDATE_HEALTH_TIMEOUT_S`, rollback on failure), `--self-check` | security tests (data_filter, health gate, --self-check) |
| A26 | Multi-app isolation | Unique container names per deployment attempt, per-deployment dirs/ports/logs/env; stopping one deployment never touches another's | E2E 3-app scenario |
| A27 | Cloudflare DNS | Idempotent CNAME ensure/delete (proxied, TTL 300), hostname regex, scoped token, graceful `dns_pending` when unconfigured | `cloudflare.test.ts` (mocked fetch, 8) |
| A28 | Tunnel ingress | `IngressProvider` interface; supervised outbound-only `cloudflared tunnel --token run` (pinned 2026.10.0, SHA-256 verified); loopback-only route targets; remote tunnel configuration is the source of truth (local `config.yml` is a diagnostic mirror); `ingress-sync` task type (18th) | 28 ingress tests |
| A29 | Domains lifecycle | `domains` table (migration 007): `requested → configuring → active → failed`, `degraded` as live-but-unverified (`active → degraded → active|failed`), `removing → removed`; periodic reconciler re-verifies against the remote tunnel config (`domain.reconcile`/`degraded`/`recovered` events); 409 on duplicate hostname; 422 tunnel-without-hostname. (2026-10-06: domain routes enforce the deployment ACL (`authorizeDeploymentAccess`); a `rollback_failed` deployment's domains are marked `failed` like any terminal deployment; the probe's IP classifier additionally denies `2002::/16` (6to4)) | `domains.test.ts` |
| A30 | Migrations 001–016 | Numbered with no gaps, parse-validated (pglast), schema.sql superset check — all in CI; chain applies cleanly to real PostgreSQL 16 in the CI `e2e` job via `npm run migrate`; runner supports `-- migrate: no-transaction` marker. `011` (project ownership: `owner_agent_id` + backfill + `project_members` + `agent_host_access`), `012` (`rollback_failed` in the `deployments.status` CHECK), `013` (`reserved_cpu`/`reserved_ram_mb` on deployments), `014` (`artifacts.manifest` + tarball-manifest verification at finalization), `015` (`superseded` deployment state, releases the reservation but stays a rollback target), `016` (`artifacts.updated_at`, required by the 001 `touch_updated_at()` trigger — found by live-PostgreSQL validation 2026-10-06) are owner-role-safe DDL (see B1) | `migration-validation` + `e2e` CI jobs |
| A31 | Dashboard | Hosts/apps/deployments/tasks panels, Approvals dossier + confirmations, service actions (restart/stop/start), SSE live, sessionStorage-only key, 401→re-auth; every dashboard-called endpoint contract-matched against real routes in CI | `check.py` + route-match CI job |
| A32 | CLI | 19 commands (`hosts apps deploy logs restart stop start status rollback domains tasks events agents projects deployments approve reject cancel secrets`), global `--json`, `UAHT_BASE_URL`/`UAHT_API_KEY` or flags, provisioning-token flag for registration | smoke 20/20 |
| A33 | JS SDK | 42/42 `node:test`; zero-dep; SSE async generator with `since`; `deploy()` helper; `rotateKey`, `tailLogs`, `getLogs`, `addDomain(..., ingress?)` | local run 2026-10-06 |
| A34 | Python SDK | Parity with JS (`since` forwarded, `on_event` callback, `rotate_agent_key`, `tail_logs`, `add_domain`); 43/43 locally 2026-10-06 | local run 2026-10-06 |
| A35 | E2E chains | 6/6 worker E2E (fake control plane over real HTTP + subprocess-backed containers: full chain, agent disappearance, manual gate, broken app, health-fail rollback, multi-app) + 21/21 API flow tests + 12/12 final-acceptance steps | local runs 2026-10-05/06 |
| A36 | Compose lifecycle parity | `restart`/`stop`/`start`/`logs`/`status` handlers resolve a compose deployment (`compose_project`) to `compose restart/stop/up/logs/ps` equivalents; GC collects old compose generations (`compose down`); boot reconcile recreates a missing compose stack from the persisted `compose_file` (stack with no compose file left is reported missing, never deleted) | handler tests, `test_reconcile.py` |
| A37 | Host scheduler + atomic reserve | `POST /v1/deployments` accepts `host_id: null`; `src/lib/scheduler.ts` selects an eligible host (online, not draining, capabilities, CPU/RAM headroom, host targeting ACL) and the deployment row always carries a concrete `host_id`; no eligible host → 503 `no_capacity`. Check+reserve is atomic: candidate host rows are locked `FOR UPDATE` inside the request transaction (PostgreSQL row locks are the serialization mechanism — no in-memory locking) | `hostSelection.test.ts`, `concurrentDeploy.test.ts` |
| A38 | Agent lifecycle (operator) | `POST /v1/agents/:id/suspend|resume|revoke` behind `requireOperator` (provisioning token bearer or agent with `admin`); self suspend/revoke → 403; suspend takes effect immediately; revoke replaces `api_key_hash` with an unmatchable random value (permanent, never resumable); events `agent.suspended`/`agent.resumed`/`agent.revoked` carry no secrets; tasks remain durable (claimed work keeps running, new requests from suspended agents rejected at auth) | `agentLifecycle.test.ts` |
| A39 | Project ownership + host targeting ACL | Migration `011`: `projects.owner_agent_id` (backfilled from the legacy `owner` name; unresolvable owners recorded in `migration_reports`, never silently assigned), `project_members`, `agent_host_access`. Central `src/lib/authz.ts` (`authorizeProjectAccess/Deployment/Artifact/Task/HostAccess`, `listAccessibleProjectIds`) enforced on the projects, deployments, tasks, artifacts, secrets, services, and domains routes; `POST /v1/projects/:id/owner` (operator only) and `GET|POST|DELETE /v1/projects/:id/members`. Legacy-open rule: `owner_agent_id IS NULL` projects and hosts with zero access rows stay accessible to all agents | `projectAcls.test.ts`, `ownerMigration.test.ts` |

Provisioning facts preserved: one token (`UAHT_PROVISIONING_TOKEN`), two
gates — agent registration via `X-Provisioning-Token` header, host
registration via `X-Provisioning-Token` header **or** `Authorization:
Bearer <token>` (hands-off first boot) **or** an agent key with `deploy`.
Worker `DeploymentStore` uses a monotonic per-store `seq` for generation
ordering (ties broken with `created_at`).

---

## Category B — IMPLEMENTED BUT REQUIRES LIVE INFRASTRUCTURE VALIDATION

The code exists and is unit/integration-tested, but the proof needs real
infrastructure this sandbox does not have: a live Supabase/PostgreSQL, a
persistent Linux host with Docker + systemd, and real Cloudflare
credentials. Each item maps to `docs/e2e-live-checklist.md`.

| # | What needs live proof | Why local tests can't prove it | Checklist |
|---|---|---|---|
| B1 | Migration chain as a **non-superuser owner role** | CI applies the chain to PG 16 as the `postgres` **superuser**. Statement-level analysis says only `001`'s `CREATE EXTENSION pgcrypto` needs elevation and `002`–`012` need only table ownership (table owner can run every statement, incl. the `005` REVOKE, the `008` constraint rebuild, the `011` ALTERs/indexes/ACL tables, and the `012` constraint rebuild — all triggers/functions) — but the chain has never been executed as a non-superuser role | #1 |
| B2 | `FOR UPDATE SKIP LOCKED` under real concurrency | pg-mem cannot plan it; the atomic claim is proven functionally via the worker E2E's faithful in-memory plane, and the CI e2e job proves single-claim exclusivity (2nd claim → 204) — real multi-host contention still untested | #1, #14 |
| B3 | Real Docker daemon | Image builds, container isolation, `--memory`/`--cpus` enforcement, real `docker ps` for reconcile, port bind behavior — the E2E uses a subprocess-backed fake docker client | #4, #7, #8, #9, #10 |
| B4 | Real systemd | `Restart=always` across `kill -9` and across a real reboot; `StartLimitBurst` behavior; reconcile running on a real boot | #10, #11 |
| B5 | Real Cloudflare | CNAME ensure/delete against the real API; `cloudflared tunnel --token run` against real edge; end-to-end HTTPS through a real domain | #12 |
| B6 | Supabase connection shape | The API holds a persistent `LISTEN uag_events`; transaction-mode poolers do not support LISTEN (the 5s backstop poll keeps events flowing, at ~5s SSE latency). Use the direct connection or a session-mode pooler — documented in `docs/deployment.md` §1 | #1 |
| B7 | Dashboard UI in a real browser | Static checks + dashboard↔route contract matching run in CI; **no functional browser test has ever exercised the UI** | — (manual) |
| B8 | Sweeper wall-clock timing | Lease expiry / degraded / offline transitions are logic-tested; real timing against wall clock needs the live system | #14 |
| B9 | Domain reconciler on real PG | The `degraded` CHECK defect (old C1) was fixed by migration `008` (the constraint now allows `'degraded'`), so live validation is no longer gated on a code fix. Still needs live proof on real PostgreSQL: pg-mem does not enforce CHECKs, so the local suites cannot prove the CHECK behaves on the real engine | #12 |

**Sign-off rule:** all 14 checks in `docs/e2e-live-checklist.md` green +
the local suites + 12-job CI green = every acceptance criterion that can
be proven is proven.

---

## Category C — REMAINING WORK

Real gaps or accepted limitations. Nothing here is a defect in what exists;
each is documented in the code or docs as a deliberate boundary or future
work.

| # | Item | Status |
|---|---|---|
| C1 | ~~DEFECT (code, reported — not fixed by WS-J):~~ **RESOLVED (WS-D, migration 008):** migration `007_domains_lifecycle.sql`'s CHECK on `domains.status` omitted `'degraded'` while `routes/domains.ts` transitions to it on the live reconciler path. Migration `008` drops and re-adds the constraint with `'degraded'` included. Live proof on real PostgreSQL (CHECK enforcement) is still outstanding — see B9. | fixed; pending live proof |
| C2 | Code-signing for self-update (offline key) | Explicitly future work. Current trust model is documented: "the control plane said so, and the SHA-256 matches" — a compromised control plane can push worker code (accepted orchestrator trust boundary) |
| C3 | HMAC request signing / replay protection | Explicitly future work, deliberately not invented mid-phase; mitigations in place: TLS-everywhere, rotation endpoints |
| C4 | Aggregate capacity accounting per host | Not implemented; `environment-update` drops memory/cpus on recreate (documented in `docs/troubleshooting.md`) |
| C5 | TCP/process/container health-check types | HTTP-only by design (A15); additional check types are future work |
| C6 | Per-resource ACL | **Partially implemented (2026-10-06):** projects now carry `owner_agent_id` + a `project_members` ACL, and hosts carry a per-agent `agent_host_access` targeting ACL (see A39; legacy-open rule keeps pre-ACL rows accessible). Still not implemented: per-deployment scoping beyond the project/deployment ACL that exists. Permissions remain global per agent key on top of this |
| C7 | Secret scrubber floor | Values < 4 chars are not redacted from logs (documented; avoids false positives on ordinary log text) |
| C8 | Quarantine unbounded | Mismatched-artifact quarantine dir has no size cap (minor operational gap) |
| C9 | Manual-approval notification path | Agents must poll `?status=awaiting_approval`; no push/notification channel |
| C10 | Python SDK suite: 1 failing test | `test_rotate_host_token_posts_grace_and_adopts_new_token` asserts the literal `"Bearer <redacted>"` while the client correctly sends the real token — test-authoring bug, reported to the coordinator |
| C11 | `environment-update` on compose deployments | `handle_environment_update` recreates a *container*; a compose deployment has no `container_name`, so the handler raises `HandlerError("deployment has no container to update")`. Rewriting the compose file's `environment:` and re-`up`ing the stack is not implemented. **Workaround:** redeploy with the new env. Applies to compose only — single-container deployments update fine. | not implemented |

Out of scope by design (not work): tunnel-token provisioning and
dashboard-tunnel public-hostname entries stay human steps; the tunnel
token is visible in the host's process table to local root (accepted,
documented).

---

## Phase log (narrative — no competing scores)

- **Phases 2–5** — reconnect backoff/jitter, stale-host sweeper, post-reboot
  reconcile, retry classification + claim leases, port registry, multi-app
  isolation, compose rollback, GC, SDK/CLI surface (`getLogs`, approve/
  reject, `domains`), JS `since` fix.
- **Phase 6** — security findings 1–18 resolved or explicitly accepted
  (replay protection, code-signing); `extra_args` removed; registration
  gated on `UAHT_PROVISIONING_TOKEN`; artifact scoping + wire caps;
  tar-slip/data_filter; secrets delivery to hosts.
- **Phase 7** — honest ingress: DNS≠reachability; worker-managed
  `cloudflared` tunnel (outbound-only, pinned binary); remote tunnel
  configuration as source of truth; `ingress-sync` task type.
- **Phase 9** — deterministic E2E: 6 worker scenarios + 21 API flow tests;
  caught and fixed 3 real integration bugs (dispatcher `claimed`
  re-report, `_ProgressStreamer._stop` shadowing, missing
  `artifact_checksum`/`project_name` injection).
- **Phase 10** — documentation finalization; `docs/deployment.md` and
  `docs/troubleshooting.md` added.
- **W16** — adversarial security audit (F1–F4, W1–W2 fixed; see
  `docs/security-audit.md`).
- **W18** — final audit: 7 flow traces + 30-step acceptance (28 pass,
  2 blocked on live infra); post-audit rollback GC-window fix
  (verify-before-teardown + rebuild-from-contract).
- **WS-J (2026-10-06)** — docs re-verified against code (same pass as the
  rewrite above).
- **WS4 follow-up (2026-10-06)** — rollback unified into
  `deployments/rollback.py` (real post-restore health check, port
  settlement on both paths); domain-probe SSRF hardened (fail-closed
  all-address validation + IP pinning); manifest-level `volumes`
  rejected; compose lifecycle parity (restart/stop/start/logs/status,
  GC, boot-reconcile); docker re-audit (context_dir confinement,
  image-tag flag guards, self-update version traversal); new
  `docs/live-acceptance.md` (smoke procedure + §19 acceptance
  checklist); docs corrected (migration chain `001–010`, C1 resolved).
- **Final Remaining Fixes pass (2026-10-06)** — audit-and-fix, smallest
  changes only: agent suspend/resume/revoke behind `requireOperator`
  (new `admin` permission) with `agent.suspended/resumed/revoked`
  events; project ownership + host targeting ACLs
  (`owner_agent_id`, `project_members`, `agent_host_access`,
  migration `011`, central `src/lib/authz.ts` wired into seven route
  modules); host scheduler for `host_id: null` deploys (atomic
  check+reserve via `SELECT … FOR UPDATE`, 503 `no_capacity` when no
  host is eligible); rollback failure durability (`rollback_failed` /
  `partially_reconciled` persisted honestly, migration `012`, same-port
  explicit rollback fix, port settlement on both paths, reconcile
  pre-pass for interrupted rollbacks); `tryClaim` no longer stamps
  `assigned_to`; `2002::/16` (6to4) added to the probe's denied IPv6
  ranges; doc sweep (this document: A3/A8/A16/A18/A29/A30/C6, new
  A37–A39, migrations `001–016`, duplicate C2 row removed).

---

## External prerequisites (for the coordinator)

Things that need the operator's live infrastructure, mapped to the
acceptance items they block:

1. **Supabase project (or any real PostgreSQL 14+)** — blocks B1
   (non-superuser chain validation), B2 (real claim contention), B6
   (LISTEN over the real network), B8 (sweeper timing), C1 verification
   (the `degraded` fix must be proven against real CHECK enforcement),
   and checklist #1.
2. **Persistent Linux host with Docker + systemd** — blocks B3 (real
   container lifecycle), B4 (restart/reboot behavior), and checklist
   #3, #4, #7, #8, #9, #10, #11, #14.
3. **Cloudflare API token + zone + domain** — blocks B5 and B9 (real
   DNS, tunnel run, end-to-end HTTPS), checklist #12.

A real browser (for B7, the dashboard UI) is the fourth, minor one.
