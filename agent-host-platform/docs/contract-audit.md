# Contract Conformance Audit (W15, 2026-10-05)

> **Note 2026-10-06 (WS-J):** this is the W15 historical record and stands
> as written, with one correction: the "41 types" event count it records
> was under-counted — the server actually emits **46** types (the audit
> missed `auth.failed`, `host.worker_outdated`, `domain.degraded`,
> `domain.recovered`, `domain.reconcile`). Current docs (`api.md`,
> `agent-integration.md`, `implementation-status.md`) list the verified
> 46; PROTOCOL §3.8 still needs the same sync.
>
> **Correction 2026-10-06 (WS-H2):** the PROTOCOL §3.8 sync is now done —
> the canonical list there is 46 (`auth.failed`, `host.worker_outdated`,
> `domain.degraded`, `domain.recovered`, `domain.reconcile` added, changelog
> entry dated 2026-10-06), and the stale "still needs the same sync"
> notes in `docs/api.md` and `docs/agent-integration.md` are removed.

Scope: spec §51 (API contract audit), §52 (DB contract), §46 (Supabase
privilege reality). The **code is authoritative** — `control-plane/api/src/routes/*.ts`
plus `middleware/auth.ts` and `lib/stateMachine.ts` define behavior; PROTOCOL.md,
docs, SDKs, and the CLI were brought into line where they disagreed. Nothing
here changes the wire: every code edit is SDK/CLI surface only.

## What was checked

For **every endpoint** in the control plane, method, path, auth, request
body, response body, error codes, status codes, idempotency behavior, and
required permissions were compared across six surfaces:

- `agent-sdk/protocol/PROTOCOL.md` (canonical contract doc)
- `control-plane/api/src/routes/` (ground truth) + `middleware/auth.ts` + `lib/stateMachine.ts`
- JS SDK (`agent-sdk/javascript/src/client.js`)
- Python SDK (`agent-sdk/python/src/uaht_sdk/client.py`)
- CLI (`cli/src/agent_host_cli/main.py`)
- Tests (API vitest suite, both SDK suites, CLI smoke test)

Also checked: DB columns referenced by production code vs. the migration
chain `001_initial.sql` … `007_domains_lifecycle.sql`; task/deployment/domain
states used in code vs. the DB `CHECK` constraints; and every migration for
elevated-privilege requirements (superuser / service_role).

## Fixes — PROTOCOL.md (doc was wrong, code was right)

- **§1 permissions:** listed `read_logs` and `remove` — neither exists.
  Real set (7): `deploy`, `read_status`, `restart`, `stop`, `manage_secrets`,
  `manage_domains`, `approve_deployments`.
- **§3.2 approve side effect:** `POST /v1/tasks/:id/approve` also flips the
  linked deployment to `approved` and emits `deployment.approved` — was
  undocumented (`routes/tasks.ts` `decideApproval`).
- **§3.3 claim body:** documented `{host_id, capabilities?[]}` — the code
  reads capabilities from the host row; the body is `{host_id}` only.
- **§3.5 artifacts auth:** `POST /v1/artifacts/init` and
  `PUT /v1/artifacts/:id/content` require the **`deploy`** permission, not
  just "an agent key". Permission annotations added to the projects
  endpoints as well (`POST`/`PUT` need `deploy`, reads need `read_status`).
- **§3.6 deployments:** `GET /v1/deployments` supports `?limit=` (was
  undocumented); added the missing host-token endpoint
  `POST /v1/deployments/:id/settle-rollback-ports` with
  `{target_deployment_id}` → emits `deployment.ports_settled` (called by the
  worker after a completed rollback; host must own the deployment).
- **§3.8 events:** canonical list replaced with the **41 types the server
  actually emits** (verified by extracting every `appendEvent` type plus the
  ternary/mapped emitters in `worker.ts`, `hostSweeper.ts`, `taskSweeper.ts`).
  16 missing types added (`agent.key_rotated`, `task.retrying`,
  `task.requeued`, `deployment.rollback_requested`,
  `deployment.rollback_failed`, `deployment.ports_settled`,
  `service.crash_loop`, `service.removed`, `host.token_rotated`,
  `host.degraded`, `domain.requested`, `domain.active`, `domain.failed`,
  `domain.removed`, `domain.remove_failed`); 6 never-emitted types removed
  (`agent.disconnected`, `deployment.building`, `deployment.healthcheck`,
  `healthcheck.passed`, `healthcheck.failed`, `worker.updated` — zero
  references in the codebase).
- **§2 cursor semantics:** "cursor is opaque" was wrong for events — the
  events cursor is an integer event id (`cursor must be an event id`,
  `routes/events.ts`); only the tasks cursor is opaque base64url
  (`routes/_helpers.ts`).
- **§3.10 domains:** lifecycle corrected to
  `requested → configuring → active | failed`, `removing → removed` on
  detach (migration 007; the old `dns_pending`/`error` names are gone);
  added `GET /v1/domains/:hostname`.
- **Changelog:** entry dated 2026-10-05 recording all of the above as
  doc corrections, no wire changes.

## Fixes — docs/api.md

- Register rows said "— (one-time bootstrap)": `POST /v1/agents/register`
  actually needs the `X-Provisioning-Token` header (bootstrap only for the
  very first agent when `UAHT_PROVISIONING_TOKEN` is unset, else 403);
  `POST /v1/hosts/register` needs the provisioning token (Bearer) **or** an
  agent key with `deploy` (`provisioningOrDeploy`).
- `POST /v1/tasks` auth said "`deploy` for deploy-type" — it is a per-type
  permission map (`permissionForTaskType` in `routes/tasks.ts`); the map is
  now spelled out.
- Cancel row now states the real rule: `deploy` permission **or** the task's
  creating agent.
- Artifact init/content rows now say `deploy`; added the approve side
  effect note.
- Deployments: added `?limit=` on the list row and the missing
  `POST /v1/deployments/:id/settle-rollback-ports` row (host token).
- Canonical event list synced to the same 41 types as PROTOCOL §3.8.

## Fixes — SDK parity (both SDKs) + tests

- `createDeployment` / `create_deployment` accepted no `host_port` although
  `POST /v1/deployments` has supported it since Phase 3 (fixed host port
  reservation, 409 on collision). Added in both SDKs, and passed through
  the `deploy()` convenience helpers.
- `updateProject` / `update_project` exposed only `configuration`; the wire
  PUT accepts `configuration`, `repository`, `runtime`. Both now expose all
  three (backwards compatible: `(id, configuration)` still works).
- **Dead parameters removed** (never implemented server-side, deliberately
  unpaginated by the code): `limit`/`cursor` on `list_projects` /
  `listProjects`; `limit`/`cursor` on `list_artifacts` / `listArtifacts`;
  `cursor` on `list_deployments` / `listDeployments`. The server ignores
  them; the SDKs no longer pretend they work.
- `list_services` / `listServices` gained the real `limit` the server
  honors (`GET /v1/services?host_id=&limit=`).
- Parity tables in both test suites now include `getDomain` / `get_domain`
  (both SDKs implement it; it was missing from the tables).
- New contract tests: 8 in JS (`test/client.test.js`), 8 in Python
  (`test/test_client.py`) covering host_port wiring, the full PUT
  project shape, the removed dead params, the services limit, and the
  deploy-helper passthrough.

## Fixes — CLI

- `deploy` gained `--host-port` (passed to `create_deployment`; errors fast
  when used without `--host`, matching the server's 422).
- `projects update` gained `--repository` and `--runtime` (previously it
  forced `--configuration`); at least one of the three is now required.
- `projects list --limit` removed — the flag was dead (the server ignores
  it, and the SDK method no longer accepts it).
- Smoke test extended: new `--host-port` deploy case and a
  `--repository/--runtime` projects-update case (both under `--json`,
  still valid JSON).

## DB contract (§52)

- The migration chain 001 → 007 is **complete**: every column the API code
  references exists in the chain (verified by a script comparing code
  references against the migrations). No new migration needed.
- Task, deployment, and domain states used in code all exist in the DB
  `CHECK` constraints; `lib/stateMachine.ts` matches the constraints.
- **Drift found (report only, not fixed):** `database/schema/schema.sql` is
  missing `artifacts.status` (added by migration 002). It is marked
  read-only by this workstream — applying schema.sql from scratch would
  break artifact upload. The migration chain is the source of truth.

## Privilege findings (§46)

- **005 `005_events_truncate_block.sql`** — **corrected 2026-10-06:** the
  earlier revision installed an event trigger to block `TRUNCATE`; that
  approach is impossible (PostgreSQL does not support event triggers on
  `TRUNCATE` — the migration failed on real PG 16). It is now a plain
  `REVOKE TRUNCATE ... FROM PUBLIC` (table owner can run it; no superuser
  needed). Full protection requires separate table ownership — documented
  in the migration header and `docs/security.md` ("events table
  ownership").
- **001 `001_initial.sql`** — needs **superuser** on a fresh database for
  `CREATE EXTENSION "pgcrypto"` (the migration header does not say so;
  migrations are never edited, so the note lives in
  `docs/deployment.md`). Alternative safe path: enable pgcrypto in the
  Supabase dashboard → Database → Extensions before running migrations.
- **006, 007** — plain DDL, no privilege issues.
- **New preflight:** `control-plane/api/src/db/migrate.ts` now exports
  `checkMigrationPrivileges(pool, file)`, called by `runMigrations` before
  each unapplied migration. For 005 it probes `current_setting('is_superuser')`;
  for 001 it probes `pg_extension` for pgcrypto. On a missing privilege it
  throws a precise error naming the migration and the safe path (SQL editor
  → mark applied → restart). It **never silently skips** a security
  migration. Probe failures fall through so the migration's native error
  surfaces instead of a misleading preflight message.

## Code quirks noted (not changed)

- `workerRouter` is mounted at **both** `/v1/hosts` and `/v1/worker`
  (`index.ts`): every worker endpoint answers under both prefixes. The docs
  describe the canonical split (heartbeat under `/v1/hosts/:id`, claim /
  progress / secrets / domains under `/v1/worker/...`); the duplicate mount
  is intentional compatibility, not a doc bug.
- The `POST /v1/tasks` permission map is enforced in code
  (`permissionForTaskType`) — the old api.md one-liner was the only wrong
  surface.

## Test counts

| Suite | Before | After |
|---|---|---|
| API vitest (`control-plane/api`) | 18 files / 276 tests | 18 files / 276 tests (unchanged — no API code behavior changed) |
| Python SDK pytest | 30 | **38** (+8 contract tests) |
| JS SDK (`npm test`, node:test) | 31 | **39** (+8 contract tests) |
| CLI smoke (`test/smoke_help.py`) | 20 help / 29 JSON | 20 help / **31** JSON (+2 new invocations) |

TypeScript compiles clean (`npm run build`); all suites green.

## Addendum — WS-H final hardening pass (2026-10-06, spec §44/45/46/47/48/72/75)

SDK/CLI/dashboard surface only; no wire changes, no control-plane edits.

### SDK parity fixes (both SDKs)

- `registerAgent` / `register_agent` sent `Authorization: Bearer <apiKey>`
  but the control plane gates `POST /v1/agents/register` on the
  **`X-Provisioning-Token` header** — registration could never succeed
  through the SDK in gated mode. Both now accept `provisioningToken` /
  `provisioning_token` and send the header.
- Constructor `apiKey` is now **optional** in both SDKs (no `Authorization`
  header when omitted) — bootstrap/registration flows run before any key
  exists. Existing keyed callers are unaffected.
- **New** `registerHost` / `register_host` → `POST /v1/hosts/register`
  (`{host, host_token}`, shown once; provisioning token via
  `X-Provisioning-Token` header, or the constructor key when it carries
  `deploy`).
- **New** `rotateHostToken` / `rotate_host_token` → `POST
  /v1/hosts/:id/rotate-token` (`{grace_seconds}` → `{host_token}`); the
  client adopts the new token on success, mirroring `rotateKey`.
- Parity tables in both test suites extended (`registerHost`,
  `rotateHostToken` / `register_host`, `rotate_host_token`); 3 new contract
  tests per SDK covering the provisioning header, the no-key constructor,
  and host-token adoption.

### CLI fixes

- `agents register` no longer requires `UAHT_API_KEY` (it never worked —
  the endpoint needs the provisioning token, not an agent key). New global
  `--provisioning-token` flag / `UAHT_PROVISIONING_TOKEN` env.
- New `agents rotate` (own API key rotation).
- `hosts` gained `register` (provisioning token or deploy key) and `get`
  actions (was list-only).
- `tasks` gained `status --task <id>` (was list-only).
- `domains` gained `get --hostname <h>` (lifecycle row).
- Smoke test extended: FakeClient stubs for the new methods, 6 new JSON
  invocations (36 total), `_client` stub accepts the new `require_key`
  kwarg.

### Dashboard (§46/47)

- New **Domains** panel: deployment picker + lifecycle table (hostname,
  status, ingress, DNS/tunnel/HTTPS flags, error) from the real
  `GET /v1/domains?deployment_id=` endpoint, on the 10s refresh cadence.
- New **Logs** panel: deployment picker + Fetch button mints one real
  `logs` task (`POST /v1/tasks`) and polls it to terminal, mirroring the
  CLI `logs` command; Stop cancels the poll. User-triggered, not
  auto-polled (each fetch is one task).
- New **endpoint-consistency test** in `dashboard/check.py`: parses the
  backend route table out of `control-plane/api/src` (router mounts,
  nested mounts, the direct `app.put` for artifact content) and verifies
  every frontend call site in `js/app.js` (api/apiWrite/get-helper/PANELS
  paths/EventSource) against it by method + path, with `:param` segment
  wildcards on both sides. Fails closed on drift.

### §72 worker polling audit (read-only; worker code owned by WS-B)

- Claim loop long-polls `POST /v1/worker/tasks/claim?wait=` (default 25s,
  server holds ≤30s, 204 on empty); heartbeat every 30s (`HEARTBEAT_INTERVAL`,
  min 5s). Consecutive failures back off exponentially 5s → 300s cap with
  ±25% jitter (`agent/backoff.py`); all waits go through `stop_event.wait`
  (SIGTERM-responsive). Idle per-host load ≈ 5 req/min against a 600
  req/min host bucket — no fleet request storm. No changes needed.

### §75 agent-neutrality / no-hardcoded-agent checks

- `grep -rni grok` over the repo: matches only vendored `node_modules`
  binaries (vitest, mime-db); zero source references.
- SDKs carry no agent-type special-casing (`type` is an opaque label;
  auth flows from `permissions` only — see `docs/agent-integration.md` §1).
- §39: `deploy()` / `createDeployment` return `{deployment, task}` with
  ids, so an agent can go offline after submitting and track work later;
  `wait` defaults to false.
