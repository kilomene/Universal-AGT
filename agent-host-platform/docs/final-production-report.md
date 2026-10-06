# Universal-AGT — Final Production Report (§81)

**Date:** 2026-10-06
**Branch:** `main` (public repo `kilomene/Universal-AGT`)
**Scope:** the 81-section "Final Production Completion, Hardening & Validation Specification"
**Method:** 10 specialist workstreams audited the actual code (never the docs alone), fixed concrete defects with the smallest possible change, added regression tests, and ran the full local suites. The coordinator fixed cross-cutting gaps and performed final verification.

**Headline test results (local, this pass):**
| Suite | Result |
|---|---|
| Host worker pytest | 634 passed, 2 skipped |
| Control Plane API vitest | 400 passed (30 files) |
| `tsc --noEmit` | clean |
| JS SDK (`node --test`) | 44 passed |
| Python SDK pytest | 43 passed |
| CLI smoke (`smoke_help.py`) | OK — 20 help invocations, 19 subcommands, 37 commands valid JSON |
| Installer validator (`test-install.sh`) | 42 passed |
| Dashboard static check (`check.py`) | pass |
| Secrets sweep | clean |
| Build-consistency check | 9/9 pass |

---

## Follow-up hardening pass (2026-10-06, WS4)

After the §81 pass landed, a follow-up hardening pass (workstreams
WS1–WS4) fixed the remaining production issues and corrected the docs.
Code changes were verified against the implementation; **no new local
test runs were executed by WS4** (the counts below are the suites the
code workstreams ran against their changes). Nothing was pushed —
the coordinator pushes once at the end.

### Files changed (code)

- `host-worker/deployments/rollback.py` *(new)* — the single rollback
  implementation both paths now share.
- `host-worker/deployments/pipeline.py` — automatic rollback (on
  healthcheck failure) now calls the unified rollback module instead of
  its own inline logic; build `context_dir` confined under the artifact
  extract dir.
- `host-worker/executor/handlers.py` — explicit `handle_rollback` now
  calls the unified rollback module; `restart`/`stop`/`start`/`logs`/
  `status` resolve compose deployments (`compose_project`) to the
  compose equivalents instead of raising.
- `host-worker/deployments/gc.py` — garbage collection covers compose
  generations (`compose down` on collected generations).
- `host-worker/deployments/reconcile.py` — a compose stack missing at
  boot is recreated with `compose up` from the persisted `compose_file`
  (a stack whose compose file is also gone is reported missing, never
  deleted).
- `host-worker/docker/client.py` — image-tag flag-like guards
  (dash-led/empty image values rejected).
- `host-worker/updater/self_update.py` — the advertised version (a
  path component of `<work>/updates/<version>`) is validated as a
  single safe component (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`),
  closing the path-traversal steering vector.
- `control-plane/api/src/routes/domains.ts` — the domain HTTPS probe
  (`probeHttps`) now resolves A+AAAA itself, requires **every**
  resolved address to be public (fail closed), and pins the validated
  IP into the TLS connection (custom `lookup`; SNI/Host keep the
  hostname) — closing the DNS-rebinding window. Informational only,
  never blocks domain creation.
- `host-worker/deployments/manifest.py` — manifest-level `volumes`
  explicitly **rejected** with a clear error (compose is the persistence
  path; accepted-but-inert would be silently misleading).

### Bugs and security issues fixed

- **Divergent rollback paths.** Automatic and explicit rollback had
  separate implementations with different guarantees. Now one module:
  verify-before-destroy on both paths, a **real** health check on the
  restored target (unhealthy recorded honestly, never assumed), and
  `POST /v1/deployments/:id/settle-rollback-ports` reconciling port
  reservations on both paths (a settle failure is recorded, never
  fatal).
- **SSRF: probe DNS-rebinding.** The pre-fix probe validated the
  hostname but connected through the system resolver at dial time, so a
  rebinding DNS answer could steer the probe at a private address.
  Fixed: fail-closed all-address validation + IP pinning.
- **Compose lifecycle parity.** `restart`/`stop`/`start`/`logs`/`status`
  raised on compose deployments; GC ignored compose generations;
  reconcile never recreated missing compose stacks. All three fixed.
- **Docker flag-injection re-audit.** `build.context` now confined to
  the artifact extract dir; image-tag flag-like guards added.
- **Self-update version path traversal.** The control-plane-advertised
  version feeds path components; now validated as a single safe
  component.
- **Volumes silently ignored.** Manifest-level `volumes` is now an
  explicit validation error; PROTOCOL §4 documents the rule and the
  compose persistence path.
- **Docs typo.** `agent-guide.md`: `UAGT_*` → `UAHT_*` (repo-wide grep
  confirms no `UAGT_` references remain in docs).

### Tests added

- `host-worker/tests/test_rollback_unified.py` *(new, 10 tests)* —
  both rollback paths pinned through the common module: real health
  check on the restored target (healthy AND unhealthy recorded
  honestly), port settlement on the automatic path, settle failure
  recorded not fatal, verify-before-destroy on both paths, GC'd target
  rebuilt from contract.
- `control-plane/api/test/securityAuditW16.test.ts` — F3 probe tests
  extended: fail-closed on any non-public A/AAAA answer, pinned lookup
  dials the validated public IP (DNS-rebinding defense), redirects
  still not followed.
- `host-worker/tests/test_handlers_rollback.py` — updated to cover the
  unified-module delegation in the explicit rollback path.

### Known limitations (documented honestly)

1. **Compose `environment-update` still raises** (`HandlerError:
   "deployment has no container to update"`) — the handler recreates a
   container and a compose deployment has none; rewriting the compose
   file's `environment:` and re-`up`ing the stack is not implemented.
   Workaround: redeploy with the new env. Tracked as C11 in
   `docs/implementation-status.md`.
2. **Live infrastructure still required** for the Category B items:
   Supabase non-superuser migration-chain execution (CI applies as
   superuser); real host/Docker/systemd install + reboot recovery;
   Cloudflare token + zone + domain for the live tunnel and public-HTTPS
   tests. The coordinator **does not have the Cloudflare token** — it is
   the operator's prerequisite (`docs/live-acceptance.md` §1).

### Docs changed (WS4, this addendum's author)

- `README.md` — migrations `001…010` (was `001…007`); new
  `docs/live-acceptance.md` row in the docs table.
- `agent-sdk/protocol/PROTOCOL.md` — §4 documents the `volumes`
  rejection and the compose persistence path.
- `docs/implementation-status.md` — rollback row (A16) rewritten for
  the unified implementation; new A36 (compose lifecycle parity);
  C1 defect marked **RESOLVED** by migration `008` (it was a fixed
  limitation still listed as unresolved); B9 unblocked on the code
  side (live proof on real PG still outstanding); new C11
  (compose `environment-update`); A30 migration chain `001–010`.
- `docs/architecture.md` — rollback section rewritten for the unified
  module (replaces the old two-path "Compose rollback" paragraph).
- `docs/deployment.md` — migration table gains `008`/`009`/`010`;
  verification query and production checklist corrected to `001–010`.
- `docs/e2e-live-checklist.md` — migrations `001–010`; canonical
  `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ZONE_ID` names (were
  `CF_API_TOKEN`/`CF_ZONE_ID`).
- `docs/security-audit.md` — 2026-10-06 follow-up note: probe
  fail-closed all-address validation + IP pinning (DNS-rebinding fix).
- `docs/live-acceptance.md` *(new)* — the §18 production smoke-test
  procedure (11 steps against real Ubuntu host + Docker + worker +
  PostgreSQL/Supabase + Cloudflare + domain), every required env var
  (`DATABASE_URL`, `UAHT_PROVISIONING_TOKEN`, `CLOUDFLARE_API_TOKEN`,
  `CLOUDFLARE_ZONE_ID`, `TUNNEL_INGRESS_HOSTNAME`, worker config —
  placeholders only, no credentials), the §19 final acceptance
  checklist (control plane / host / deployment / agent resilience /
  networking), and the known limitations.
- `docs/agent-guide.md` — `UAGT_*` → `UAHT_*` typo fix.
- `docs/host-install.md` — reviewed; no changes needed.

### Live-infra prerequisites (unchanged from §10, restated)

1. Supabase project (or any Postgres 14+) — blocks non-superuser
   chain validation, real contention claims, `LISTEN` shape, sweeper
   timing.
2. Persistent Linux host with Docker + systemd — blocks real installer
   run, Docker lifecycle, reboot recovery, self-update end-to-end.
3. Cloudflare API token + zone + domain — blocks live tunnel route
   configuration, public HTTPS, DNS lifecycle. **The operator supplies
   these; the coordinator has no token.**

## Final Remaining Fixes pass (2026-10-06 — spec §§26/§28, workstream 5)

Audit-and-fix only, smallest changes: nothing added beyond the spec
sections, nothing redesigned. Code workstreams implemented §§2/4–13/17;
this addendum records what changed, where it is proven, and what the
docs now say.

### What changed (code — other workstreams)

- **§2 SSRF IP classification** — verified correct; ADDED `2002::/16`
  (6to4) to the denied ranges in
  `control-plane/api/src/routes/domains.ts`.
- **§§4–7 Rollback durability** — single
  `host-worker/deployments/rollback.py` used by both paths; same-port
  explicit rollback fixed (verify tolerates the current deployment's
  own bind, teardown-before-restore); rollback failures persist
  `rollback_failed` / `partially_reconciled` durably (never false
  success); port registry settled via settle-rollback-ports on both
  paths; reconcile pre-pass handles interrupted rollbacks;
  `rollback_failed` added to `GCABLE_STATUSES`, the worker ingress
  `TERMINAL_DEPLOYMENT_STATUSES`, the control-plane
  `failDomainsForDeployment` triggers, `NON_ROUTABLE_DEPLOYMENT_STATUSES`,
  and the `deployments.status` CHECK (migration
  `012_rollback_failed_status.sql`).
- **§8 Host scheduler** — `POST /v1/deployments` accepts
  `host_id: null`; new `src/lib/scheduler.ts` selects an eligible host
  (online, not draining, capabilities, CPU/RAM headroom, host targeting
  ACL); the deployment row always gets a concrete `host_id`; no eligible
  host → 503 `no_capacity`.
- **§9 Atomic check+reserve** — host rows locked `SELECT … FOR UPDATE`
  inside the request transaction (documented in `scheduler.ts`).
- **§10 Central authorization** — `src/lib/authz.ts`
  (`authorizeProjectAccess/Deployment/Artifact/Task/HostAccess`,
  `listAccessibleProjectIds`) wired into the projects, deployments,
  tasks, artifacts, secrets, services, and domains routes. Legacy-open
  rule: `owner_agent_id IS NULL` projects and hosts with zero
  `agent_host_access` rows stay accessible to all agents.
- **§11 Ownership migration** —
  `011_project_ownership_acls.sql`: `projects.owner_agent_id` (backfilled
  from the legacy `owner` name; unresolvable owners recorded in
  `migration_reports`, never silently assigned), `project_members`,
  `agent_host_access`.
- **§12 Agent lifecycle** —
  `POST /v1/agents/:id/suspend|resume|revoke` behind `requireOperator`
  (provisioning token or an agent with the new `admin` permission); self
  suspend/revoke → 403; revoke replaces `api_key_hash` with an
  unmatchable random value; events `agent.suspended` / `agent.resumed` /
  `agent.revoked` carry no secrets; tasks remain durable.
- **§13 Claim pin fix** — `tryClaim` no longer stamps `assigned_to`
  (now exclusively the agent-requested pin); requeued tasks after a host
  death are claimable by other hosts.
- **§17** — volumes remain explicitly rejected in `agent.deploy.json`
  (no change).
- **Everything else in §§13–19, §§22–24** — verified already-fixed, no
  changes.

### Status of this pass's items

**IMPLEMENTED + TESTED** (local suites, no new infra needed):
scheduler selection + 503 `no_capacity` (`hostSelection.test.ts`,
`concurrentDeploy.test.ts`); agent suspend/resume/revoke + `admin`
permission (`agentLifecycle.test.ts`); project ownership/ACL
(`projectAcls.test.ts`, `ownerMigration.test.ts`); rollback failure
durability + migration 012 (`rollbackFailure.test.ts`,
`rollbackFailedMigration.test.ts`); claim `assigned_to` fix
(`recoveryLeases.test.ts` structural claim-SQL test); 6to4 denied
range (`domainsProbeSsrf.test.ts`).

**IMPLEMENTED + REQUIRES LIVE VALIDATION**: host scheduler contention
between real concurrent deploys (`FOR UPDATE` semantics under real
multi-host load — Category B2 class); migration `011`/`012` executed as
a non-superuser owner role on a real PostgreSQL (Category B1 class);
`rollback_failed` CHECK enforcement on real PG (pg-mem does not enforce
CHECKs — Category B9 class); end-to-end agent suspend → running tasks
keep their hosts' results readable (needs a live host).

**NOT IMPLEMENTED** (still): finer per-deployment scoping beyond the
project/host ACLs that now exist; push/notification channel for
`awaiting_approval`; HMAC request signing / replay protection;
code-signing for worker self-updates; non-HTTP health-check protocols.

### Statements this pass corrects (supersede earlier sections)

- The §16 "GENUINELY REMAINING WORK" item **"Agent suspend/revoke API
  endpoint" is now IMPLEMENTED** (operator endpoints + `admin`
  permission, §12 above).
- **"Per-resource (per-project/per-deployment) ACLs" is now partially
  implemented**: projects have `owner_agent_id` + `project_members`,
  hosts have `agent_host_access` targeting ACLs (§§10–11). Only
  finer-than-project scoping remains future work.
- The event journal count in §16 and elsewhere is **49**, not 46
  (`agent.suspended`/`agent.resumed`/`agent.revoked` added).

### Docs changed (this addendum's author, §26)

- `agent-sdk/protocol/PROTOCOL.md` — new changelog entry; §1
  permissions now 8 (`admin`); §3.1 agent lifecycle endpoints; §3.5
  owner/members endpoints; §3.6 scheduler + `no_capacity` +
  `rollback_failed`; §3.8 events 46 → 49; `assigned_to` semantics note.
- `docs/api.md` — agent lifecycle rows; project members/owner rows;
  scheduler + `no_capacity` on `POST /v1/deployments`; `rollback_failed`
  status; events 49; `no_capacity` error code.
- `docs/security.md` — `admin` permission; agent lifecycle section;
  project ownership/ACL section; event journal trio.
- `docs/deployment.md` — migration table `011`/`012`; all
  `001…010`/`002–007` claims corrected to `001…012`/`002–012`; muse
  operator example gains `"admin": true`.
- `docs/cloudflare.md` — `rollback_failed` in the domain-failover
  terminal list; 6to4 in the probe's denied IPv6 set.
- `docs/architecture.md` — scheduler paragraph; `assigned_to` claim
  note; `rollback_failed` terminal branch.
- `docs/agent-integration.md` — events 46 → 49 + lifecycle trio.
- `docs/implementation-status.md` — A3 (8 permissions), A8
  (`assigned_to`), A16 (rollback durability), A18 (49 events), A29
  (domains ACL + 6to4), A30 (migrations `001–012`), new A37 (scheduler),
  A38 (agent lifecycle), A39 (project/host ACL), B1 (`002–012`), C6
  (per-resource ACL now partial), duplicate C2 row removed.
- `docs/live-acceptance.md` — migrations `001–012`; events 49.
- `README.md` — migrations `001…012`; layout `001_initial …
  012_rollback_failed_status`.
- `control-plane/api/README.md` — endpoint map (lifecycle, members,
  owner, scheduler); bootstrap example fixed to real permissions
  (was `read_logs`/`remove`, which do not exist).

### Code fixes from the §28 quality sweep (this pass)

- `host-worker/agent/config.py` — removed dead `import shlex`
  (verified unused; module imports cleanly).
- `host-worker/agent/context.py`, `host-worker/health/collector.py` —
  removed unused `Optional` from the `typing` imports.
- `agent-host-platform/database/schema/schema.sql` — added the three
  tables migration `011` creates (`project_members`,
  `agent_host_access`, `migration_reports`) plus their indexes,
  `projects.owner_agent_id`, and the `rollback_failed` CHECK value
  (migration `012`). The schema previously missed them, which fails
  CI's migration-validation superset check; re-ran that exact check
  locally — schema.sql now covers all 13 tables created by the chain.
- Marker sweep (`TODO`/`FIXME`/`HACK`/`XXX`/`not implemented`): clean —
  all hits are legitimate test doubles (`vi.stubGlobal`, harness stubs)
  or genuine error paths; no `throw new Error("not implemented")`
  anywhere. No duplicate rollback code (single `rollback.py`), no
  duplicate authorization code (single `authz.ts`), no `grok`
  hard-coding, no secrets in new event payloads or error messages,
  status codes consistent (503 `no_capacity`, 403 self-action, 409
  conflicts, 404 not-found).

---

## 1. Every file changed (111 files this pass)

### Database (4)
- `agent-host-platform/database/migrations/008_task_queue_hardening.sql` *(new)*
- `agent-host-platform/database/migrations/009_heartbeat_enrichment.sql` *(new)*
- `agent-host-platform/database/migrations/010_registration_idempotency.sql` *(new)*
- `agent-host-platform/database/schema/schema.sql`

### Control Plane API — src (11)
- `agent-host-platform/control-plane/api/src/lib/cloudflare-tunnel.ts`
- `agent-host-platform/control-plane/api/src/lib/config.ts`
- `agent-host-platform/control-plane/api/src/lib/domainReconciler.ts` *(new)*
- `agent-host-platform/control-plane/api/src/lib/errors.ts`
- `agent-host-platform/control-plane/api/src/lib/events.ts`
- `agent-host-platform/control-plane/api/src/lib/hostSweeper.ts`
- `agent-host-platform/control-plane/api/src/lib/idempotency.ts`
- `agent-host-platform/control-plane/api/src/lib/taskSweeper.ts`
- `agent-host-platform/control-plane/api/src/middleware/auth.ts`
- `agent-host-platform/control-plane/api/src/routes/agents.ts`
- `agent-host-platform/control-plane/api/src/routes/deployments.ts`
- `agent-host-platform/control-plane/api/src/routes/domains.ts`
- `agent-host-platform/control-plane/api/src/routes/hosts.ts`
- `agent-host-platform/control-plane/api/src/routes/tasks.ts`
- `agent-host-platform/control-plane/api/src/routes/worker.ts`
- `agent-host-platform/control-plane/api/src/index.ts`

### Control Plane API — tests (17)
- `test/cloudflareTunnel.test.ts`, `test/concurrentDeploy.test.ts`, `test/config.test.ts`,
  `test/configConsistency.test.ts`, `test/domainReconcile.test.ts` *(new)*,
  `test/domains.test.ts`, `test/e2eFlows.test.ts`, `test/recoveryCloudflare.test.ts`,
  `test/recoveryDb.test.ts`, `test/recoveryLeases.test.ts`,
  `test/registrationIdempotency.test.ts` *(new)*, `test/secretsRoutes.test.ts`,
  `test/security.test.ts`, `test/securityAuditW16.test.ts`,
  `test/taskQueueHardening.test.ts` *(new)*, `test/workerVersions.test.ts`
  (+ `test/migrations.test.ts` schema coverage where touched)

### Host worker — src (9)
- `agent-host-platform/host-worker/agent/config.py`
- `agent-host-platform/host-worker/agent/main.py`
- `agent-host-platform/host-worker/agent/api.py`
- `agent-host-platform/host-worker/deployments/pipeline.py`
- `agent-host-platform/host-worker/deployments/compose_validate.py`
- `agent-host-platform/host-worker/docker/client.py`
- `agent-host-platform/host-worker/health/checker.py`
- `agent-host-platform/host-worker/ingress/cloudflared.py`
- `agent-host-platform/host-worker/ingress/__init__.py`
- `agent-host-platform/host-worker/logs/store.py`
- `agent-host-platform/host-worker/.env.example`

### Host worker — tests (15)
- `tests/test_drain.py` *(new)*, `tests/test_ws_f_hardening.py` *(new)*,
  `tests/test_log_retention.py` *(new)*, `tests/test_tunnel_token.py` *(new)*,
  `tests/test_resilience_agent_gone.py` *(new)*, `tests/test_resilience_concurrent.py` *(new)*,
  `tests/test_resilience_cp_outage.py` *(new)*, `tests/test_resilience_failure_matrix.py` *(new)*,
  `tests/test_resilience_network.py` *(new)*, `tests/test_resilience_worker_crash.py` *(new)*,
  `tests/test_deployment_contract.py` *(new)*, `tests/test_handlers_rollback.py` *(new)*,
  `tests/test_reconcile.py` *(new)*, `tests/test_rollback_gc_window.py` *(new)*,
  `tests/test_self_update.py` *(new)*
- modified: `test_ingress.py`, `test_phase4.py`, `test_resources.py`,
  `test_compose_security.py`, `test_config_env.py`

### SDKs / CLI / dashboard (10)
- `agent-host-platform/agent-sdk/javascript/test/client.test.js`
- `agent-host-platform/agent-sdk/javascript/README.md`
- `agent-host-platform/agent-sdk/python/src/uaht_sdk/client.py`
- `agent-host-platform/agent-sdk/python/test/test_client.py`
- `agent-host-platform/agent-sdk/python/README.md`
- `agent-host-platform/agent-sdk/protocol/PROTOCOL.md`
- `agent-host-platform/cli/src/agent_host_cli/main.py`
- `agent-host-platform/cli/test/smoke_help.py`
- `agent-host-platform/cli/README.md`
- `agent-host-platform/dashboard/check.py`, `dashboard/index.html`,
  `dashboard/js/app.js`, `dashboard/css/style.css`

### Installer / CI / repo root (5)
- `agent-host-platform/scripts/install-host.sh`
- `agent-host-platform/scripts/test-install.sh`
- `.github/workflows/ci.yml`
- `scripts/check-build-consistency.sh` *(new)*
- `README.md`

### Docs (12)
- `agent-host-platform/docs/implementation-status.md` (full §65 rewrite)
- `agent-host-platform/docs/deployment.md` (new §9 Backup & restore)
- `agent-host-platform/docs/security.md`, `docs/CONFIG.md` *(new canonical-config doc)*,
  `docs/api.md`, `docs/agent-integration.md`, `docs/agent-guide.md`,
  `docs/cloudflare.md`, `docs/contract-audit.md`, `docs/e2e-live-checklist.md`,
  `docs/host-install.md`, `docs/security-audit.md`
- `agent-host-platform/control-plane/api/README.md`
- this file: `agent-host-platform/docs/final-production-report.md` *(new)*

---

## 2. Every production issue fixed

### Task queue, leases, idempotency (WS-D)
- **D1 — Task transitions lacked compare-and-swap.** Cancel, approve/reject, progress-report, and the lease sweeper all did read-then-UPDATE with no status guard: a worker completing a task between the route's read and its UPDATE got the completed task rewritten to `cancelled`; a concurrent cancel+approve resurrected a cancelled task to `queued`; a delayed progress report moved `completed → running`; the sweeper double-counted attempts. All four now use `WHERE id=$1 AND status=$2` (409 on mismatch); the sweeper rechecks lease expiry in the UPDATE.
- **D2 — Deployment mirror corrupted by non-lifecycle tasks.** A failed `logs`/`status`/`healthcheck`/`build`/`ingress-sync` task flipped its deployment to `failed`. Now only deployment-owning task types (`deploy, rollback, restart, stop, start, remove, environment-update`) move `deployments.status`; the mirror runs under `SELECT … FOR UPDATE` in one transaction; Cloudflare-touching hooks run after commit; events fire only on actual status change; failed restart/stop now emits `deployment.failed` instead of flipping silently.
- **D3 — Cancel of a pre-running deploy leaked its port reservation forever.** New `compensateCancelledDeploy` releases `port_allocations` and fails attached domains (deployment status left `requested` per the e2eFlows contract — the suite caught an initial over-correction).
- **WS-B#1 (§37) — Draining hosts could still claim.** Claim handler 409s when `hosts.status='draining'` (or `worker_draining`); `tryClaim` carries an atomic `EXISTS` guard so a host that starts draining mid-wait can't slip through.
- **WS-B#2 — Heartbeat dropped worker-reported fields.** UPDATE now persists `capabilities`, `worker_draining`, `worker_status`, `ingress` (jsonb), `reported_host_name` (migration 009).
- **WS-C#3 (§31) — `worker.updated` never emitted** though docs promised it. Heartbeat now selects the previous `worker_version` and emits `worker.updated` on actual change.

### Secrets, auth, config, events (WS-C)
- **§59 — Startup validator was dangerously narrow** (only checked 3 vars). Now rejects: malformed `TUNNEL_INGRESS_HOSTNAME`; tunnel ingress without Cloudflare tunnel credentials (names the exact missing var); half-configured DNS (token xor zone); invalid `PUBLIC_INGRESS_HOSTNAME`/`PORT`; non-positive rate limits; conflicting heartbeat thresholds (`degraded ≥ offline`); warns on `LOG_LEVEL=debug` in production and DNS creds with no ingress target. Never prints secret values.
- **§31 — No audit events for auth failures.** `auth.failed` events (endpoint, reason, credential channel — never the value) now emitted for wrong provisioning tokens on both register routes; journal-flood bounded by the 10/min/IP unauth limiter.
- **§31 — Sweeper could diverge state from audit history.** `hostSweeper.sweepOnce` was UPDATE-then-appendEvent non-atomically. `events.ts` refactored to `insertEvent(client)` + post-commit `publishEvent`; sweep is now BEGIN → UPDATE → INSERT → COMMIT → publish, ROLLBACK on failure.
- **§33 — No request correlation IDs.** `X-Request-Id` honored (validated or UUID), logged as `req_id`, echoed as response header; activates the `request_id` field in `sendError` (§70).
- **§3 — Canonical names missing from `.env.example`/CONFIG.md** (`CLOUDFLARE_TUNNEL_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `RATE_LIMIT_UNAUTH_PER_MIN`, `UAHT_ROTATE_RATE_PER_MIN`, `CLOUDFLARE_API_BASE` added; `configConsistency.test.ts` enforces coverage).

### Installer & host lifecycle (WS-B)
- **D1 — Import-coverage gate was a dead branch.** `python3 … <<EOF || true` forced exit 0, so the "new package forgotten from installer" check always passed (proved with a scratch package). Replaced with `if python3 …` (`set -e`-exempt condition); validator now genuinely fails (exit 1) on a forgotten package. Suite is 42/42.
- **D2 — Installer crashed on the documented default path.** `WORKER_TUNNEL_TOKEN=$UAHT_TUNNEL_TOKEN` under `set -u` with no default → `unbound variable` abort on every plain install. Defaulted `UAHT_TUNNEL_TOKEN=""`, `UAHT_INGRESS_ENABLED="0"`.
- **D3 — Reinstall minted a duplicate host and blanked the tunnel secret.** New `reuse_existing_credentials()` (reuses stored id/token; loud WARNING on control-plane URL mismatch) and `preserve_tunnel_token()` (never blanks a stored tunnel token; explicit input wins). 5 new validator scenarios.
- **D4 — Provisioning failure was an unhandled traceback.** Now `fail "host provisioning failed (POST …/v1/hosts/register)"`.
- **D5 — Provisioning omitted the ingress capability.** Capabilities now append `"ingress"` when `UAHT_INGRESS_ENABLED=1`.
- **D6 (§37) — Worker ignored operator-set draining.** New `apply_server_state()` latches `ctx.draining` from the heartbeat's `host.status`; `claims_paused()` (server latch OR sticky `WORKER_DRAINING=true`); `claim_loop` skips claiming while draining; heartbeats continue; in-flight work drains via pool shutdown.
- **D7 (§9) — Heartbeat payload missing §9 fields.** New `enrich_heartbeat_payload()`: `host_name`, `worker_version`, `worker_status`, `draining`, `capabilities`, `ingress` (no tokens; test asserts no secrets). `WORKER_DRAINING` added to generated `worker.env` and `.env.example`.

### Deployment pipeline, resources, ports (WS-E)
- **D1 (§11) — Resource admission was check-then-act.** New transactional `reserve_resources()`: a process-wide lock combines persisted reservations with in-flight reservations from concurrent deploys; raises `DeployError` when the request doesn't fit; released on persist or any failure path. **D1b:** reservations leaked on §4–§5 failures (docker-build/compose) — try/except now releases both resource and port reservations and re-raises.
- **D2 (§13) — `artifact_id` not persisted** in deployment state. Now stored on both state saves in `deploy()`.
- **D3 (§20) — Reconcile never reported unexpected containers.** New `_report_unexpected()`: containers claimed by no deployment state are reported, never destroyed.
- **D4 (§21) — Rollback gaps.** Target ports verified free *before* teardown (refuses when squatted); restored target is health-checked post-restore with honest `health_status`; `verify_host_port_free` fixed for TIME_WAIT false-positives (SO_REUSEADDR).
- **D5 (§22) — Self-update failure handling.** Download/corrupt-archive errors now wrapped in `UpdateError` (release dir cleaned); rollback-restart returncode checked.
- **D6 (§34) — No log retention.** `LogStore.prune()` + `WORKER_LOG_RETENTION_DAYS` (default 30): task logs older than the window deleted; deployment logs only when the deployment is gone from the store (fail-closed). Wired into the heartbeat loop (every 20th heartbeat, ~10 min).
### Artifact & Docker security (WS-F)
- **D1 (§16) — No decompression-bomb protection.** `extract_archive()` now enforces `MAX_EXTRACT_BYTES = 8 GiB` (absolute, not ratio — a ratio cap was verified to break legitimate artifacts) and `MAX_ARCHIVE_MEMBERS = 100_000`, with an incremental tar header scan so a bomb's payload is never even streamed.
- **D2 (§16) — `artifact_id` used unvalidated as a filesystem path** (agent-controlled; `makedirs()` ran before the server's UUID check → arbitrary directory creation). New `validate_artifact_id()` (`^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$`) enforced at all 4 construction sites.
- **D3 (§16) — `verify_checksum` raised unhandled `TypeError`** on non-hex digest input. Strict 64-hex check → clean `DeployError`.
- **D4 (§17) — `DockerClient.run()` image positional.** Dash-led/empty image values rejected (defense-in-depth against flag injection).
- **D5 (§17) — Compose validator gaps.** `runtime:` (custom OCI runtime) and `userns_mode: host` now denied.
- **D6 (§18) — Compose project names not isolated.** `uaht-{safe_project}` let "My App" and "my-app" share a stack. Now `uaht-{safe_project}-{project_id[:8]}`; legacy stacks torn down on redeploy.
- **D7 (§19) — Health checker followed HTTP redirects** (SSRF oracle: 302 to arbitrary URL, boolean leaked reachability). Now `allow_redirects=False`; only exact 200 counts as healthy.
- **D8 (§34) — `cloudflared.log` grew forever.** 10 MiB rotation, 1 generation, same policy as `logs/store.py`.

### Cloudflare & domains (WS-G)
- **D1 (§25) — Tunnel token on the process command line** (`cloudflared tunnel --token <secret> run` — world-readable via `ps`). Token now passed via the `TUNNEL_TOKEN` environment variable (natively supported); argv carries no secret.
- **D2 (§24/§26/§51) — No `degraded` state; no reconciliation; DB could permanently claim `active` while the route was gone.** New `domainReconciler` (interval driver, default 300s): `verifyTunnelRoutes()` re-reads the authoritative remote config; unverifiable routes → `degraded` + `domain.degraded`; healed rows → `active` + `domain.recovered`; degraded rows retried via `provisionDomain` (skipped when remote state couldn't be confirmed, so outages don't flap rows to `failed`). `collectDesiredRoutes()` keeps `degraded` rows routed.
- **D3 (§24) — `requested → failed` transition missing**: `failDomainsForDeployment` 409'd mid-loop, stranding sibling domains. Fixed.
- **D4 (§24) — `setDomainStatus` check-then-act race.** Optimistic `AND status = $from` guard; loser gets 409.
- **D5 (§26) — `removeDomain` skipped DNS teardown** when the CF DNS API was unconfigured, marking `removed` while a live CNAME stayed orphaned. Now refuses with 502 (stays `removing`).
- **D6 (§27) — Worker `_check_route` accepted `target_host="localhost"`** (can resolve to `::1`). Tightened to exact `127.0.0.1`.

### SDKs / CLI / dashboard (WS-H/H2)
- Python SDK test fixture tripped the secrets sweep (`client.api_key = "uagh_old"` → renamed to the filtered placeholder `uagh_old_not_real`).
- **CLI `apps --host <name>` sent the name as `host_id`** (`main.py` `cmd_apps`) — the backend 400s unless `host_id` is a UUID. Now resolves name→id via `_resolve_host_id` first.
- **Python SDK `get_domain` didn't URL-encode the hostname** (JS SDK used `encodeURIComponent`) — now `quote(hostname, safe='')`; JS/Python parity test added.
- **`docs/agent-integration.md` documented `GET /v1/projects?limit=&cursor=`** — the backend ignores both params. Doc corrected; the honored `&limit=` on `GET /v1/services` documented.
- **PROTOCOL §3.8 said 41 event types; the server emits 46** — list synced + changelog; stale-sync notes removed from `api.md`/`agent-integration.md`.
- Agent-neutrality verified end-to-end: zero `grok` source references, no Tailscale, no Muse-only DB logic; Instinct uses the identical contract.

### Stale-pending artifact reaper (coordinator, §16/§60)
- **Gap: an artifact `init`ed but never PUT stayed `pending` forever** with no cleanup path (a retry with the same project+version 409s against the unique constraint). New `src/lib/artifactSweeper.ts`: every 300s (configurable) it fails `pending` rows older than 24h (configurable) with an `artifact.upload_failed` event (`reason: 'abandoned'`), transactionally (insert-in-tx, publish-after-commit) with a CAS guard so a concurrent upload completion wins the race. Wired into `index.ts` start/stop.

### Registration idempotency (coordinator, §7)
- **Gap: `POST /v1/hosts/register` and `POST /v1/agents/register` were not idempotent.** A retried register after a lost response 409'd with no credential recovery path. Both routes now accept an optional `idempotency_key` (migration 010): same key + equal params → 200 replay with a **fresh** credential (original is hash-only and unrecoverable; host replay uses a 300s rotation grace, agent replay matches `/me/rotate` immediate semantics) + `idempotent_replay: true`; same key + different params → 409; no key → unchanged behavior. 7 new tests.

### Worker version visibility (coordinator, from WS-A audit)
- **Worker heartbeat never sent `worker_version`** — after a self-update the plane's stored version went stale and `worker_outdated` detection used the old value. `enrich_heartbeat_payload` now includes it.
- **Worker ignored the `worker_outdated` signal.** `apply_server_state` now logs a loud once-per-occurrence warning when the plane flags the worker outdated.

### Docs (WS-J)
- `implementation-status.md` fully rewritten to the §65 three-category format (the old 451-line doc had five disagreeing status tables).
- `deployment.md` gained §9 Backup & restore (§35).
- Corrected: permission registry (7 enforced names — `read_logs`/`remove` grant nothing), event list (46 emitted, was "41"), migration list (001…007), CI job count (12), domain lifecycle (`degraded`), `UAGT_API_KEY` → `UAHT_API_KEY` typo in two guides, stale "runners unavailable" note.

---

## 3. Every new feature implemented
- Registration idempotency (`idempotency_key` on host/agent register + replay-via-rotation).
- Domain `degraded` lifecycle state + periodic reconciler (`domainReconciler`, 300s default).
- Host `draining` state: worker-side claim pause + server-side claim refusal.
- `worker.updated` event emission on version change.
- `auth.failed` audit events.
- Request correlation IDs (`X-Request-Id` → `req_id` → `x-request-id` response header).
- Transactional resource reservation with in-flight tracking.
- Decompression-bomb and member-count caps on archive extraction.
- Tunnel token via environment (never argv).
- Build-consistency gate (`scripts/check-build-consistency.sh`, 9 checks).
- Stale-pending artifact sweeper (fails abandoned uploads with audit events).
- Production config validator extended (tunnel/DNS/rate-limit/heartbeat/log-level checks).
- Backup & restore procedure (documented, code-path-validated).

## 4. Every migration added/changed
- **008_task_queue_hardening** *(new)*: `degraded` added to `domains.status` CHECK (fixes C1 — the code's reconciler transitions to it but 007's CHECK omitted it; pg-mem doesn't enforce CHECKs so suites stayed green while real PG would 500); `domains.idempotency_key` unique (§7); `tasks.type` 18-type CHECK + `attempts>=0` + `max_attempts>=1` (§5/§49); `deployments.health_status` CHECK; 3 hot-path indexes (§73: `idx_tasks_claimable` partial matching claim ORDER BY, `idx_tasks_claimed_by`, `idx_tasks_payload_deployment_id`).
- **009_heartbeat_enrichment** *(new)*: `worker_draining`, `worker_status`, `ingress` (jsonb), `reported_host_name` columns for the enriched heartbeat (`capabilities` already existed).
- **010_registration_idempotency** *(new)*: nullable `idempotency_key` on `hosts`/`agents` + partial unique indexes.
- **005** unchanged (REVOKE TRUNCATE — the event-trigger approach is impossible on PostgreSQL; must never be reintroduced).
- `schema.sql` synced with 008/009/010 (canonical-schema contract).

## 5. Every security issue addressed
- Tunnel token removed from process argv (env only); never logged/returned/stored in git (verified).
- Decompression bombs, archive path traversal, symlink/hardlink/device escapes, `artifact_id` path injection — all rejected with tests (40-test malicious-archive suite).
- Docker flag injection surface closed: dash-led images rejected; compose `runtime:`/`userns_mode: host` denied; compose project-name isolation; bind mounts confined.
- Health-check SSRF oracle closed (no redirect following).
- Domain SSRF: routes terminate only at `127.0.0.1:<port>`; `localhost` rejected worker-side; private ranges, metadata endpoints, wildcards rejected both sides.
- Auth: `auth.failed` auditing; CAS on all task transitions; host/agent credential isolation kept; rotation grace (hosts) / immediate (agents) with audit events.
- Secrets: AES-256-GCM verified; no secret in logs/events/API payloads/dashboard (test-asserted); secrets sweep clean on the tree.
- Rate limits reviewed: unauth 10/min, agent 120/min, host 600/min (heartbeats ~1/30s — far below), rotation 10/min per credential.

## 6. Every test added/changed
- **API (new files):** `test/taskQueueHardening.test.ts` (24: CAS, mirror gating, cancel compensation, rollback/domain idempotency, degraded lifecycle, C1 proof, drain refusal, heartbeat enrichment, `worker.updated`, request_id), `test/registrationIdempotency.test.ts` (7), `test/domainReconcile.test.ts` (8, fake-Cloudflare double).
- **API (extended):** `test/security.test.ts` (+7: provisioning dual-header, auth.failed ×3, request-id ×3), `test/config.test.ts` (+12), `test/hostSweeper.test.ts` (+3), `test/configConsistency.test.ts`, `test/cloudflareTunnel.test.ts`, plus test-schema patches for new columns across 12 files.
- **API (new):** `test/artifactSweeper.test.ts` (6: stale-pending fail + event, fresh rows untouched, non-pending untouched, idempotence, config defaults/overrides).
- **Worker (new files):** `test_drain.py` (24), `test_ws_f_hardening.py` (40), `test_log_retention.py`, `test_tunnel_token.py` (14), `test_resilience_agent_gone.py`, `test_resilience_concurrent.py`, `test_resilience_cp_outage.py`, `test_resilience_failure_matrix.py`, `test_resilience_network.py`, `test_resilience_worker_crash.py`, `test_deployment_contract.py`, `test_handlers_rollback.py`, `test_reconcile.py`, `test_rollback_gc_window.py`, `test_self_update.py`.
- **Worker (extended):** `test_ingress.py` (+5), `test_phase4.py`, `test_resources.py`, `test_compose_security.py`, `test_config_env.py`.
- **Clients:** JS SDK `client.test.js` extended; Python SDK `test_client.py` (fixture fix); CLI `smoke_help.py` extended (37 commands JSON-valid).

## 7. Full CI result
**GREEN — run 37423891504, commit `b54767b6`, 2026-10-06:** all 12 jobs pass:
| Job | Result |
|---|---|
| Control Plane API (tsc + vitest) | ✅ success |
| Host Worker (pytest) | ✅ success |
| Security tests (API + host worker) | ✅ success |
| JS SDK (node:test) | ✅ success |
| Python SDK (pytest) | ✅ success |
| CLI (smoke) | ✅ success |
| Dashboard (static check) | ✅ success |
| Migrations (numbering + parse + schema coverage) | ✅ success |
| E2E smoke (API + real PostgreSQL 16) | ✅ success |
| Installer validation (dry-run, no root) | ✅ success |
| Build consistency (no committed output, clean source build, version coherence) | ✅ success |
| Secrets sweep | ✅ success |

## 8. Live infrastructure tests performed
None could be executed: no persistent Linux host, no Supabase project, and no Cloudflare API token were available to this pass (see §10). Everything below was verified against real components locally (real PostgreSQL 16 in CI's e2e job, real API server + pg-mem suites, real worker logic, labeled fake-Cloudflare doubles).

## 9. Cloudflare tests performed
- 87/87 pass across the 7 control-plane CF/domain suites using a clearly-labeled fake Cloudflare API double (`test/fakeCloudflare.ts`) + pg-mem: route create/update/delete, duplicate hostname 409, deployment-removal teardown, rollback, degraded→heal, unknown-remote-rule preservation policy (409 on claim conflict), DNS-teardown refusal.
- Worker: 21/21 (`test_tunnel_token.py`: token never in argv, rotation semantics, strict loopback).
- **Not performed (needs token + domain):** real `PUT …/cfd_tunnel/{id}/configurations` shape, public HTTPS reachability, DNS propagation, live `cloudflared` reading `TUNNEL_TOKEN`.

## 10. Remaining external prerequisites
1. **Supabase project (or any Postgres 14+)** — blocks: non-superuser migration-chain execution (CI's e2e applies as superuser; only 001/pgcrypto needs elevation, documented), `LISTEN uag_events` shape (needs direct/session connection, NOT the transaction pooler), sweeper wall-clock timing, `FOR UPDATE SKIP LOCKED` contention behavior.
2. **Persistent Linux host with Docker + systemd** — blocks: real installer run, worker registration/heartbeat against live CP, Docker deployment/health/rollback, resource admission against real cgroups, VM-reboot recovery, worker self-update end-to-end.
3. **Cloudflare API token + zone + domain** — blocks: live tunnel route configuration, public HTTPS test, DNS lifecycle, route-removal verification (§§66/67).
4. (Minor) A real browser for dashboard UI validation beyond the static endpoint check.

## 11. Intentionally unsupported behavior
- Non-HTTP health checks (only HTTP 200-on-`127.0.0.1:<port>` is a health gate; TCP/other protocols report running-vs-unknown).
- Agent key suspension/revocation API (DB `agents.status` supports `suspended`/`revoked`, but no endpoint mints that transition — rotation is the available control).
- Code-signing of worker updates (checksum-pinned, not signed).
- `worker_draining` local flag is sticky by design (operator clears via config); server-advertised draining latches/clears per heartbeat.
- The local `config.yml` tunnel mirror is diagnostic only — the remote Cloudflare config is authoritative for token-managed tunnels.

## 12. Exact production deployment procedure (control plane)
1. Provision Postgres 14+ (Supabase: use the **direct/session** connection string — `LISTEN uag_events` does not work through the transaction pooler).
2. Set env (see §14). `DATA_ENCRYPTION_KEY` (64-hex) and `UAHT_PROVISIONING_TOKEN` must be generated once and stored in the operator's secret manager — key loss invalidates all stored secrets.
3. `npm ci && npm run build && npm start` (builds from source deterministically; `start` rebuilds `dist/`).
4. Migrations run automatically on boot (`src/db/migrate.ts`); 001 needs `CREATE EXTENSION pgcrypto` privilege once (Supabase: allowlisted, run from the SQL editor).
5. Serve behind TLS (Cloudflare or a reverse proxy); point the dashboard at the API.

## 13. Exact host installation procedure
1. On a clean Linux host (Docker required for app hosting): `curl` the installer or copy `agent-host-platform/scripts/install-host.sh`.
2. Set `UAHT_CONTROL_PLANE_URL`, `UAHT_HOST_NAME`; optionally `UAHT_TUNNEL_TOKEN` + `UAHT_INGRESS_ENABLED=1` for public ingress.
3. `sudo bash install-host.sh` — validates prerequisites, creates the `agent-host` user/dirs, installs Python deps, copies all worker packages, writes `/opt/agent-host/worker.env` (0600), installs + enables the systemd unit.
4. The worker provisions itself (`POST /v1/hosts/register` with `UAHT_PROVISIONING_TOKEN`), starts heartbeating, and claims queued tasks.
5. Re-running the installer reuses stored credentials (never mints duplicates, never blanks the tunnel token).

## 14. Exact environment variables required
**Control plane (all `UAHT_`-canonical; see `api/.env.example` + `docs/CONFIG.md`):**
`DATABASE_URL`, `PORT`, `ARTIFACT_DIR`, `LOG_DIR`, `DASHBOARD_DIR`, `DATA_ENCRYPTION_KEY` (64-hex, required), `UAHT_PROVISIONING_TOKEN` (required), `RATE_LIMIT_AGENT_PER_MIN` (120), `RATE_LIMIT_HOST_PER_MIN` (600), `RATE_LIMIT_UNAUTH_PER_MIN` (10), `UAHT_ROTATE_RATE_PER_MIN` (10), `HEARTBEAT_SWEEP_INTERVAL_S` (30), `HEARTBEAT_DEGRADED_AFTER_S` (90), `HEARTBEAT_OFFLINE_AFTER_S` (300), `PG_POOL_MAX` (10), `LOG_LEVEL`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, `PUBLIC_INGRESS_HOSTNAME`, `TUNNEL_INGRESS_HOSTNAME`, `CLOUDFLARE_TUNNEL_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_BASE`.
**Host worker (`WORKER_` prefix; see `host-worker/.env.example`):**
`WORKER_CONTROL_PLANE_URL`, `WORKER_HOST_NAME`, `WORKER_HOST_TOKEN`, `WORKER_HOST_ID`, `WORKER_POLL_WAIT`, `WORKER_HEARTBEAT_INTERVAL`, `WORKER_WORK_DIR`, `WORKER_APPS_DIR`, `WORKER_WORKER_VERSION`, `WORKER_DRAINING`, `WORKER_CAPABILITIES`, `WORKER_CRASH_LOOP_THRESHOLD`, `WORKER_CRASH_LOOP_WINDOW_S`, `WORKER_INGRESS_ENABLED`, `WORKER_INGRESS_PROVIDER`, `WORKER_TUNNEL_TOKEN`.
**Compatibility aliases (documented, normalized immediately, tested):** `CLOUDFLARE_TUNNEL_API_TOKEN` ← `CLOUDFLARE_API_TOKEN` fallback; `WORKER_TUNNEL_TOKEN` ← `UAHT_TUNNEL_TOKEN`.

## 15. Final architecture diagram
```
Muse / Instinct / any agent ──HTTPS──▶ Universal-AGT Control Plane ──▶ PostgreSQL/Supabase
   (temporary clients; submit &    │        (durable source of truth:        (durable task queue,
    monitor, then disappear)      │         tasks, deployments, ports,        FOR UPDATE SKIP LOCKED
                                  │         domains, secrets, events)          claims, leases, idem-
                                  │                                          potency keys)
                                  │ outbound task polling + heartbeats
                                  ▼
                         Persistent Host Worker (outbound-only, no inbound)
                                  │  agent/ (register, heartbeat, drain)
                                  │  deployments/ (pipeline, transactional ports/resources)
                                  │  executor/ + docker/ (isolated containers)
                                  │  health/ (127.0.0.1:<port> checks)
                                  │  ingress/ (cloudflared, token via env)
                                  │  updater/ (atomic self-update)
                                  ▼
                         Docker ──▶ App A, App B, App C … (isolated)
                                  ▼
                         Cloudflare Tunnel ──▶ Internet (public HTTPS)
                         (remote config authoritative)
```

## 16. Final statement — three-way split

### IMPLEMENTED AND TESTED (local suites + CI green)
Source/build consistency gate; deterministic builds; installer (42 checks incl. genuine import-coverage gate, credential reuse, no secret clobbering); config contract (one canonical name per setting, alias layers documented/tested); production config validator; durable task queue (CAS transitions, atomic claims, leases, sweeper, retry/backoff policy); idempotency (tasks, deployments, rollback, domains, host/agent registration); transactional resource reservation; transactional port allocation; deployment pipeline with safe extraction (bomb caps, traversal rejection), checksum verification, manifest validation; Docker security (flag-injection and compose-escape closes); health checks (no-redirect, exact-200); startup reconciliation; rollback (verify-before-teardown + rebuild-from-contract); worker self-update; draining (worker + server); heartbeats with full §9 payload; Cloudflare automation with degraded-state reconciliation (fake-double tested); domain lifecycle with unique ownership; SSRF protection both sides; AES-256-GCM secrets with rotation; auth isolation (agent/host/dashboard); `auth.failed` + 46-type event journal (append-only, REVOKE-protected); rate limiting; request correlation IDs; standardized errors; SDKs (JS 44 / Python 43), CLI (19 commands, 37 JSON-valid), dashboard (static endpoint check); 12-job CI.

### IMPLEMENTED BUT REQUIRES LIVE INFRASTRUCTURE VALIDATION
Real Docker daemon behavior; real systemd install/start/reboot; non-superuser migration chain on Supabase; `LISTEN` over the direct connection; `FOR UPDATE SKIP LOCKED` under real contention; sweeper wall-clock timing; live Cloudflare route/DNS/HTTPS end-to-end; live `cloudflared` with env-passed token; dashboard in a real browser; resource admission against real cgroups; VM-reboot recovery; worker self-update against a live systemd unit.

### GENUINELY REMAINING WORK
- Agent suspend/revoke API endpoint (DB supports it; no route mints the transition — needs an admin-auth design decision).
- Non-HTTP health-check protocols.
- Per-resource (per-project/per-deployment) ACLs — current model is per-agent permission flags.
- Approval notification path (awaiting_approval tasks have no push/notify channel).
- Code-signing for worker updates (checksum-pinned today).
- The 50-step live acceptance run (§66) and live Cloudflare test (§67) — blocked on §10 prerequisites, not on code.

---

# Addendum — Final Resource Scheduling, Rollback Health, and Live Integration Completion (2026-10-06)

This pass implemented the user's 30-section "Final Remaining Fixes" prompt to the letter, plus the follow-up "Final Resource Scheduling, Rollback Health, and Live Integration Completion" prompt (Parts 1–9). Audit-first: verify-only sections with no defect were left untouched; nothing was added beyond the prompts.

## A. Persistent / atomic resource reservation (Part 1)
- **New migration `013_reserved_resources.sql`**: `deployments.reserved_cpu` / `deployments.reserved_ram_mb` — the deployment row itself owns its reservation (PostgreSQL, never process memory).
- **New `lib/scheduler.ts`**: `selectHostForDeployment` runs inside the deployment-creation transaction — candidate host rows locked `FOR UPDATE`, reservations summed over deployments in resource-consuming states, capacity verified, deployment + reservation committed atomically. Two concurrent 3 CPU/8 GB requests on a 4 CPU/16 GB host → exactly one succeeds (tested).
- **Single authoritative `normalizeResources`**: `{"cpu": 1.5, "memory": "512Mi"}` → `{cpu, ram_mb}`; one parsing rule shared by scheduler and worker.
- **`isHostSchedulable(host)`**: one draining definition used consistently — `status='draining'` or `worker_draining=true` hosts are never selected; existing apps and owned tasks are untouched.
- **Worker `_resource_reservations` kept as a local in-worker concurrency guard only**; audited against double-counting with the persistent reservation.
- Reservations release on terminal states (`failed`/`removed`/`rolled_back`/`rollback_failed`) and survive worker + control-plane restarts (verified by test).
- All 17 Part-1L tests added (`test/resourceScheduling.test.ts`).

## B. Rollback health semantics (Part 2)
- **Bug fixed**: `target_healthy = outcome["target_health_status"] != "unhealthy"` treated `unknown` as success — in `executor/handlers.py:327`, `deployments/rollback.py` commit phase, and `deployments/pipeline.py` automatic-rollback path. Now: success **only** when `target_health_status == "healthy"`; `unhealthy`/`unknown` → `rollback_failed` with reason `"target health could not be verified"`.
- **Port settlement runs only after the target is proven healthy**; settle failure → durable `rollback_failed`/`partially_reconciled`, never reported as success.
- Same-port rollback preserved (teardown-before-restore, registry converges); third-party port collision → durable failure, unrelated app never overwritten.
- All 15 Part-2H regression tests added (`test_rollback_health_semantics.py`, `rollbackFailure.test.ts`).

## C. Event visibility (Part 3) — verdict: GLOBAL BY DESIGN
The event journal is intentionally globally readable (documented audit contract: `docs/security.md`, `PROTOCOL.md` §3.8, `docs/api.md`). Filtering would silently break the contract, so no filtering was added; the contract is now documented explicitly and all 49 event types were audited — **zero secret material** in any payload.

## D. ACL non-regression (Part 4)
One demonstrated IDOR bypass found and fixed: `PUT /v1/artifacts/:id/content` performed no project ACL check — any agent with `deploy` could overwrite another project's artifact bytes. Now gated by central `authorizeArtifactAccess`. All other routes verified on the central `lib/authz.ts` helpers.

## E. Agent lifecycle (Part 5) — verified
active → works; suspended → 403 immediately; resumed → works; revoked → 401 permanently (hash replaced with unmatchable random; no TTL window). **No credential cache exists**: `attachAuth` does a fresh DB lookup on every request.

## F. Security audit (Part 7)
One genuine issue fixed: `tarfile` `data_filter` (PEP 706, Python 3.12+) vs installer blessing Python ≥ 3.10 — on 3.10/3.11 the symlink/hardlink control raised `TypeError` instead of enforcing. Installer floor raised to 3.12 (CI already pins 3.12) + fail-closed capability probe in `pipeline.py`/`self_update.py`. Full pattern sweep otherwise clean (zero `shell=True`/`os.system`/`eval`/`--privileged`/socket mounts/SQL interpolation in production code).

## G. Live integration docs (Part 6)
`docs/live-acceptance.md` extended with the Part 6 architecture diagram, verified env-var/migration/endpoint/service-name references (all checked against code), and the kill-worker / Cloudflare external-network procedures — all marked **NOT YET EXECUTED**.

## H. Test results (Part 8 gate, run by coordinator)
- Control plane: `tsc` clean, **555/555 vitest** (41 files)
- Host worker: **721 passed, 2 skipped**
- JS SDK 44/44, Python SDK 43/43, CLI 20 helps + 37 JSON commands OK, installer 42/42, dashboard static checks pass, secrets sweep clean, migrations 6/6
- CI: 12/12 green (run to be recorded on push)

## I. Three-way split — updated
**IMPLEMENTED AND TESTED** (additions this pass): persistent atomic resource reservations (migration 013); `isHostSchedulable` draining rule; rollback `== "healthy"`-only success with `rollback_failed` on unknown; settle-after-healthy port ordering; artifact content-upload ACL fix; Python 3.12 floor for tar security control.
**Correction to the previous addendum's "GENUINELY REMAINING WORK"**: agent suspend/resume/revoke API and per-resource (project/deployment) ACLs are now **implemented and tested** (they were delivered in the two preceding passes) — they are removed from remaining work.
**IMPLEMENTED BUT REQUIRES LIVE INFRASTRUCTURE VALIDATION** (unchanged): real Docker daemon, real systemd install/reboot, non-superuser Supabase migration chain, live Cloudflare route/DNS/HTTPS, live `cloudflared`, VM-reboot recovery.
**GENUINELY REMAINING WORK**: non-HTTP health-check protocols; approval push-notification channel; code-signing for worker updates (checksum-pinned today); the live acceptance run — blocked on operator prerequisites (Supabase role, Ubuntu host + Docker, Cloudflare token/zone/domain), not on code.
