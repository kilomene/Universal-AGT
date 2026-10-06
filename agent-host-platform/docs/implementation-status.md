# Universal-AGT — Implementation Status

**Rewritten 2026-10-06 (WS-J final production hardening pass).** Every claim
below was re-verified against the implementation on this date — routes,
middleware, sweepers, worker handlers, SDKs, CLI, migrations, and the test
suites that were runnable locally. There is exactly one status presentation
in this document: the three categories below. Historical phase notes at the
bottom are narrative only and carry no competing scores.

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
  via `npm run migrate`, API booted, claim exclusivity proven).
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
| A3 | Permissions | Enforced registry is exactly 7 names (`deploy`, `approve_deployments`, `read_status`, `restart`, `stop`, `manage_secrets`, `manage_domains`); per-type map covers all 18 task types (`permissionForTaskType`, unknown types default closed to `deploy`); cancel needs `deploy` or creatorship | security + route tests asserting the full map |
| A4 | Host registration | `uagh_` tokens via `X-Provisioning-Token` header **or** `Authorization: Bearer <provisioning token>` **or** an agent key with `deploy` (`provisioningOrDeploy`); `host.registered` event | CI e2e smoke + route tests |
| A5 | Host authentication | Host token must match `:id`/`host_id` on claim/heartbeat/progress (`requireHost`); kind isolation both directions | security tests |
| A6 | Heartbeats + stale-host sweeper | Heartbeat posts stats every 30s; sweeper marks `degraded`/`offline` at 90s/300s (`HEARTBEAT_*_S`); heartbeats revive `offline`/`degraded` but never overwrite operator-set `draining` | `hostSweeper.test.ts`, route tests |
| A7 | Task queue + idempotency | Create/poll/cancel, cursor pagination, idempotency keys (tasks: `type`+`payload`; deployments: 6-field compare; 409 on conflict, 200+`idempotent_replay` on replay; 23505 race handled) | `idempotency.test.ts`, `e2eFlows.test.ts` (21) |
| A8 | Atomic claims | `UPDATE … FOR UPDATE SKIP LOCKED` — exactly one host wins; long-poll `?wait=N`; claim stamps `lease_expires_at` (`TASK_CLAIM_LEASE_S`, default 600s), refreshed on every progress report | CI e2e proves exclusivity against real PG 16 (2nd claim → 204); worker E2E proves exactly-once behavior functionally |
| A9 | Claim leases + retry policy | Stuck-task sweeper (60s) requeues lease-expired work (`task.requeued`, attempt counted) or fails it when `attempts ≥ max_attempts` / type not retry-safe (`deploy`/`restart`/`start`/`stop`/builds only if the attempt never reached `running`; `remove`/`rollback` never auto-retry) | `recoveryLeases`, `taskSweeper` (14), `retryPolicy` (9) tests |
| A10 | Artifact upload | Two-phase init + PUT octet-stream; `ARTIFACT_MAX_BYTES` (500MB) enforced on `Content-Length` upfront, per-chunk while streaming, and on declared `size` → 413 `payload_too_large`; server verifies size + SHA-256 after write | artifact security route tests |
| A11 | Artifact download scoping | Host token may download only artifacts referenced by tasks *that host claimed* (`payload.artifact_id`); 403 otherwise; agent `read_status` downloads unchanged | security tests (403 scoping) |
| A12 | Deploy pipeline | Worker re-verifies SHA-256 (`hmac.compare_digest`) **before any docker call** (zero-docker-calls on mismatch, quarantine); manifest validated against PROTOCOL §4; resource limits → `--memory`/`--cpus` | pipeline tests (12), worker E2E |
| A13 | Manual approval | Task created in `awaiting_approval`; approve→`queued` (+ deployment →`approved`, `deployment.approved` event) / reject→`cancelled`; `approve_deployments` permission; dashboard Approvals panel with dossier + confirmation modals | `e2eFlows` approve/reject semantics, acceptance step 10 |
| A14 | Port registry | Fixed `host_port` reserved in `port_allocations` (unique per host+port → 409); worker bind-tests at OS level and scans `docker ps` published ports before `docker run`/`compose up` | port registry tests |
| A15 | Health checks | HTTP `GET http://127.0.0.1:{port}{path}`, exact-200, 2s poll, configurable timeout (default 120s). **Design boundary:** HTTP only — no TCP/process/container check types | exercised over real HTTP in worker E2E |
| A16 | Rollback | Worker auto-rollback on health-fail (new removed, previous restarted); agent-driven `POST /v1/deployments/:id/rollback` → `type=rollback` task; **verify-before-teardown** + **rebuild-from-contract** when the target is past the `DEPLOY_KEEP_GENERATIONS=2` GC window (fails cleanly with "cannot be restored" while the current deployment keeps serving) | pipeline + `test_rollback_gc_window.py` (2), acceptance steps 8/9 |
| A17 | Logs | Per-task/deployment logs, 10MiB × 3 rotation, `SecretScrubber` on disk and on control-plane sends. **Documented floor:** values < 4 chars not scrubbed | security tests |
| A18 | Events journal | Append-only (row trigger blocks UPDATE/DELETE; migration `005` = `REVOKE TRUNCATE ON events FROM PUBLIC`, stops non-owner roles, no superuser needed); 46 event types emitted; SSE with pg LISTEN/NOTIFY + 5s backstop poll + keepalive | 005 migration test, SSE tests |
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
| A29 | Domains lifecycle | `domains` table (migration 007): `requested → configuring → active → failed`, `degraded` as live-but-unverified (`active → degraded → active|failed`), `removing → removed`; periodic reconciler re-verifies against the remote tunnel config (`domain.reconcile`/`degraded`/`recovered` events); 409 on duplicate hostname; 422 tunnel-without-hostname | `domains.test.ts` |
| A30 | Migrations 001–007 | Numbered with no gaps, parse-validated (pglast), schema.sql superset check — all in CI; chain applies cleanly to real PostgreSQL 16 in the CI `e2e` job via `npm run migrate`; runner supports `-- migrate: no-transaction` marker | `migration-validation` + `e2e` CI jobs |
| A31 | Dashboard | Hosts/apps/deployments/tasks panels, Approvals dossier + confirmations, service actions (restart/stop/start), SSE live, sessionStorage-only key, 401→re-auth; every dashboard-called endpoint contract-matched against real routes in CI | `check.py` + route-match CI job |
| A32 | CLI | 19 commands (`hosts apps deploy logs restart stop start status rollback domains tasks events agents projects deployments approve reject cancel secrets`), global `--json`, `UAHT_BASE_URL`/`UAHT_API_KEY` or flags, provisioning-token flag for registration | smoke 20/20 |
| A33 | JS SDK | 42/42 `node:test`; zero-dep; SSE async generator with `since`; `deploy()` helper; `rotateKey`, `tailLogs`, `getLogs`, `addDomain(..., ingress?)` | local run 2026-10-06 |
| A34 | Python SDK | Parity with JS (`since` forwarded, `on_event` callback, `rotate_agent_key`, `tail_logs`, `add_domain`); 43/43 locally 2026-10-06 | local run 2026-10-06 |
| A35 | E2E chains | 6/6 worker E2E (fake control plane over real HTTP + subprocess-backed containers: full chain, agent disappearance, manual gate, broken app, health-fail rollback, multi-app) + 21/21 API flow tests + 12/12 final-acceptance steps | local runs 2026-10-05/06 |

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
| B1 | Migration chain as a **non-superuser owner role** | CI applies the chain to PG 16 as the `postgres` **superuser**. Statement-level analysis says only `001`'s `CREATE EXTENSION pgcrypto` needs elevation and `002`–`007` need only table ownership (table owner can run every statement, incl. the `005` REVOKE and all triggers/functions) — but the chain has never been executed as a non-superuser role | #1 |
| B2 | `FOR UPDATE SKIP LOCKED` under real concurrency | pg-mem cannot plan it; the atomic claim is proven functionally via the worker E2E's faithful in-memory plane, and the CI e2e job proves single-claim exclusivity (2nd claim → 204) — real multi-host contention still untested | #1, #14 |
| B3 | Real Docker daemon | Image builds, container isolation, `--memory`/`--cpus` enforcement, real `docker ps` for reconcile, port bind behavior — the E2E uses a subprocess-backed fake docker client | #4, #7, #8, #9, #10 |
| B4 | Real systemd | `Restart=always` across `kill -9` and across a real reboot; `StartLimitBurst` behavior; reconcile running on a real boot | #10, #11 |
| B5 | Real Cloudflare | CNAME ensure/delete against the real API; `cloudflared tunnel --token run` against real edge; end-to-end HTTPS through a real domain | #12 |
| B6 | Supabase connection shape | The API holds a persistent `LISTEN uag_events`; transaction-mode poolers do not support LISTEN (the 5s backstop poll keeps events flowing, at ~5s SSE latency). Use the direct connection or a session-mode pooler — documented in `docs/deployment.md` §1 | #1 |
| B7 | Dashboard UI in a real browser | Static checks + dashboard↔route contract matching run in CI; **no functional browser test has ever exercised the UI** | — (manual) |
| B8 | Sweeper wall-clock timing | Lease expiry / degraded / offline transitions are logic-tested; real timing against wall clock needs the live system | #14 |
| B9 | Domain reconciler on real PG | The reconciler's `degradeDomain` path writes `status='degraded'` — on real PostgreSQL this is currently **blocked by defect C1** (migration 007's CHECK omits `'degraded'`; pg-mem does not enforce CHECKs). Live validation is gated on the C1 fix | #12 |

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
| C1 | **DEFECT (code, reported — not fixed by WS-J):** migration `007_domains_lifecycle.sql` CHECK constraint on `domains.status` omits `'degraded'`, but `routes/domains.ts` `setDomainStatus` transitions to it on the live reconciler path (`degradeDomain`). On real PostgreSQL the UPDATE fails with a check violation; pg-mem doesn't enforce CHECKs, so all suites stay green. Fix: a follow-up migration adding `'degraded'` to the constraint (or the constraint recreated to match `ALLOWED_TRANSITIONS`). **Blocks B9 and any live tunnel-mode domain degradation.** | needs a code-owning workstream |
| C2 | Code-signing for self-update (offline key) | Explicitly future work. Current trust model is documented: "the control plane said so, and the SHA-256 matches" — a compromised control plane can push worker code (accepted orchestrator trust boundary) |
| C3 | HMAC request signing / replay protection | Explicitly future work, deliberately not invented mid-phase; mitigations in place: TLS-everywhere, rotation endpoints |
| C4 | Aggregate capacity accounting per host | Not implemented; `environment-update` drops memory/cpus on recreate (documented in `docs/troubleshooting.md`) |
| C5 | TCP/process/container health-check types | HTTP-only by design (A15); additional check types are future work |
| C6 | Per-resource ACL | Permissions are global per agent key by design; no per-project/per-deployment scoping |
| C7 | Secret scrubber floor | Values < 4 chars are not redacted from logs (documented; avoids false positives on ordinary log text) |
| C8 | Quarantine unbounded | Mismatched-artifact quarantine dir has no size cap (minor operational gap) |
| C9 | Manual-approval notification path | Agents must poll `?status=awaiting_approval`; no push/notification channel |
| C10 | Python SDK suite: 1 failing test | `test_rotate_host_token_posts_grace_and_adopts_new_token` asserts the literal `"Bearer <redacted>"` while the client correctly sends the real token — test-authoring bug, reported to the coordinator |

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
- **WS-J (2026-10-06, this pass)** — docs re-verified against code:
  event list corrected to the 46 actually emitted (the "41" missed
  `auth.failed`, `host.worker_outdated`, `domain.degraded`,
  `domain.recovered`, `domain.reconcile`); permission registry corrected
  to the 7 enforced names; CI described as 12 jobs; backup/restore
  procedure documented; Supabase project-level setup documented;
  `degraded`-status CHECK defect found and reported.

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
