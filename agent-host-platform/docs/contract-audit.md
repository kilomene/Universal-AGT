# Contract Conformance Audit (W15, 2026-10-05)

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

- **005 `005_events_truncate_block.sql`** — needs **superuser** (`CREATE
  EVENT TRIGGER`; row triggers cannot block `TRUNCATE`). Documented in the
  migration header and `docs/deployment.md`. Safe path: run the file in the
  Supabase SQL editor (runs as superuser), then
  `INSERT INTO schema_migrations (name) VALUES ('005_events_truncate_block.sql')`,
  then restart the API. Re-apply after any restore (event triggers are
  not captured by schema-only dumps).
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
