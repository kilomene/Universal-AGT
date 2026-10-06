# Resource contract — Universal AGT

One canonical resource contract. The CPU and memory values used by the
scheduler, the PostgreSQL reservation, the deployment record, the worker,
and the Docker runtime are the **same** deployment resources — never two
different numbers for one deployment.

## Source of truth

The artifact's **validated `agent.deploy.json` manifest** is authoritative:

```
agent.deploy.json (in the artifact tarball)
        ↓  validate (worker grammar — lib/manifest.ts on the control plane,
        ↓           deployments/manifest.py on the worker; one grammar)
        ↓  normalize (normalizeResources / normalize_resources; one rule)
        ↓  canonical deployment contract { cpu, ram_mb }
        ↓  PostgreSQL transaction (host row FOR UPDATE → check → reserve
        ↓           → INSERT deployment with reserved_cpu / reserved_ram_mb)
        ↓  task payload carries reserved_cpu / reserved_ram_mb
        ↓  worker re-validates the tarball manifest and enforces
        ↓           tarball resources ≤ reserved contract (fail loudly)
        ↓  Docker uses the tarball manifest's resources
```

`POST /v1/artifacts/init` accepts the manifest; the control plane
validates it with the worker's grammar and stores it on
`artifacts.manifest` (migration 014). At content-upload finalization the
control plane extracts the tarball's embedded `agent.deploy.json` and
proves it describes the same contract as the init-registered manifest
*before* marking the artifact `ready`: a disagreement (in either
direction) or a registered-but-absent tarball manifest fails the upload
with `artifact.manifest_mismatch` / `artifact.manifest_missing`. When no
manifest was registered, the tarball's validated manifest is adopted onto
the row so the scheduler still reserves from the exact definition the
worker will deploy. `POST /v1/deployments` derives scheduling constraints
from that manifest. Deployments that reference a legacy artifact without
a stored manifest (or no artifact) fall back to
`projects.configuration` — the previous behavior, preserved.

The worker is the final backstop: if the tarball's manifest demands more
than the task payload's reserved contract, the deploy task fails with a
clear error instead of silently over-committing the host.

## The `superseded` lifecycle state (migration 015)

When a new deployment of the same project+host becomes healthy, the
worker stops the previous deployment's container and marks it
`superseded` locally. The control plane mirrors that in the same
transaction that marks the new deployment `running`: every *other*
`running` deployment of the same project+host moves to `superseded`.

`superseded` is deliberately **not** in the resource-consuming states —
its `reserved_cpu` / `reserved_ram_mb` stop counting against the host,
so capacity accounting can never drift (V1+V2+V3 would otherwise
triple-charge a host running only V3). The row is preserved (never
deleted) so it remains a valid rollback target; a successful rollback
restores it to `running`, re-acquiring its reservation. Its domains are
not routed while superseded (it is in
`NON_ROUTABLE_DEPLOYMENT_STATUSES`).

## Canonical representation

```json
{ "resources": { "cpu": 1.5, "memory": "512Mi" } }
```

normalizes to the single internal form:

```
{ "cpu": 1.5, "ram_mb": 512 }
```

- **cpu**: JSON number, finite, > 0. Anything else normalizes to null
  (no requirement) and is rejected by validation.
- **memory**: string matching `^\d{1,6}[mMgG][iI]?$`. All of these are
  accepted and mean the same thing:

  | written | megabytes |
  |---|---|
  | `512m`, `512Mi` | 512 |
  | `1g`, `1Gi` | 1024 |
  | `8g`, `8Gi` | 8192 |

  The grammar is identical in `lib/manifest.ts` (validation),
  `lib/scheduler.ts` `normalizeResources`, and the worker's
  `deployments/manifest.py` (`MEMORY_RE` + `normalize_resources`).
  There is exactly one resource grammar in the repository.

## Reservation invariant

The following is always true:

```
sum(reservations of non-terminal deployments on host)
  + new deployment reservation
  ≤ host capacity (total_cpu / total_ram_mb)
```

- The reservation is created **atomically with deployment creation**:
  the candidate host row is locked `FOR UPDATE`, persistent reservations
  are summed from `deployments.reserved_cpu / reserved_ram_mb` over
  resource-consuming states, capacity is checked, and the deployment row
  (carrying the reservation) is inserted — all in one transaction.
  PostgreSQL is the serialization mechanism; no in-memory locking.
- A deployment reserves its declared CPU/RAM **before** it may occupy a
  host, and the reservation stays durable until the deployment reaches a
  terminal state (`failed`, `removed`, `rolled_back`, `rollback_failed`,
  `stopped`), which releases it automatically (the row stops matching
  the consuming-state filter).
- The worker's process-local reservation remains only as a secondary
  runtime/concurrency guard inside one worker process. It is **not** a
  second source of truth and never double-counts the control-plane
  reservation.
- Draining hosts (`hosts.status = 'draining'` or `worker_draining =
  true`) are never selected for new deployments (`isHostSchedulable`,
  one definition used by the scheduler, task claiming, and the API).

## What the worker deploys

`host-worker/deployments/pipeline.py` validates the tarball's
`agent.deploy.json` (`validate_manifest`), normalizes
`resources` (`normalize_resources` — the same rule), enforces
`tarball ≤ reserved contract`, then passes the values to Docker
(`--memory`, `--cpus`). Volumes in `agent.deploy.json` are rejected
by validation (explicitly unsupported; compose is the persistence path).
