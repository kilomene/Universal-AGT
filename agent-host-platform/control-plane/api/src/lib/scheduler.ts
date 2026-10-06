// Host selection for deployments with an omitted host_id (§8) and the
// atomic check-and-reserve contract (§9), plus persistent resource
// reservations (Part 1).
//
// §8 contract (deliberate model): at request time POST /v1/deployments
// accepts {"project_id","version","host_id": null}. The control plane then
// selects a suitable persistent host — online, not draining, capabilities,
// resource availability, project/task constraints, and the agent's host
// targeting ACL — and creates the actual deployment row with a CONCRETE
// host_id. The deployment row ALWAYS has a concrete host. When no host is
// eligible the request fails fast (503 no_capacity): it is never silently
// deployed to an arbitrary host.
//
// §9 atomicity: selectHostForDeployment MUST run inside the caller's
// database transaction. Candidate host rows are locked with FOR UPDATE, so
// the eligibility check (status, draining, capabilities, CPU/RAM
// availability, host ACL) and the reservation (the deployment row INSERT,
// carrying reserved_cpu / reserved_ram_mb) commit atomically. PostgreSQL
// row locks are the serialization mechanism — there is no in-memory
// locking anywhere in this path. Concurrent deployment requests serialize
// on the host row instead of racing on stale utilization snapshots.
//
// Part 1 reservations: capacity is computed from PERSISTENT reservations —
// the sum of reserved_cpu / reserved_ram_mb over deployments in
// resource-consuming states (see CONSUMING_DEPLOYMENT_STATUSES) — never
// from the host's observed cpu_pct/ram_pct utilization snapshot. The
// reservation is snapshotted at creation and released by status transition
// to a terminal state, so it survives control-plane and worker restarts.

import type { PoolClient } from 'pg';
import { logger } from './log';

/**
 * Deployment statuses that hold a resource reservation on their host.
 *
 * These are the repo's ACTUAL deployment states (deployments.status CHECK):
 * every non-terminal state consumes. Terminal states — failed, rolled_back,
 * rollback_failed, stopped — release the reservation automatically because
 * the reservation query only sums these states. ('removed' is not a
 * control-plane deployment state; it exists only as a worker-local GC
 * generation label.)
 */
export const CONSUMING_DEPLOYMENT_STATUSES = [
  'requested',
  'approved',
  'building',
  'starting',
  'healthcheck',
  'running',
  'stopping',
] as const;

export interface HostSelectionConstraints {
  /** Requesting agent — host targeting ACL is evaluated for this agent. */
  agentId: string;
  /** Every capability the chosen host must advertise. */
  requiredCapabilities: string[];
  /** CPU cores the deployment needs (null = no requirement). */
  requiredCpuCores: number | null;
  /** RAM megabytes the deployment needs (null = no requirement). */
  requiredRamMb: number | null;
}

interface CandidateHost {
  id: string;
  status: unknown;
  worker_draining: unknown;
  capabilities: unknown;
  total_cpu: number | null;
  total_ram_mb: number | null;
}

function numOrNull(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * The repo's single draining definition, shared with the task-claim gate
 * (routes/worker.ts §37): a host is NOT schedulable while its operator-set
 * status is 'draining' OR its worker self-reports draining. Draining never
 * destroys existing apps or owned tasks — it only stops NEW scheduling.
 */
export function isHostSchedulable(host: {
  status?: unknown;
  worker_draining?: unknown;
}): boolean {
  if (host.status === 'draining') return false;
  if (host.worker_draining === true) return false;
  return true;
}

/**
 * Single authoritative resource parser (Part 1B) — byte-identical semantics
 * to the worker's deployments.manifest.normalize_resources:
 *
 *   {"cpu": 1.5, "memory": "512Mi"} -> {cpu: 1.5, ram_mb: 512}
 *
 * cpu: finite number > 0 (else null). memory: string matching
 * /^\d{1,6}[mMgG][iI]?$/ ("256m", "512Mi", "1g", "2Gi") -> megabytes
 * (else null). Never throws: malformed/absent fields normalize to null so
 * a bad manifest can never crash scheduling — validation is the separate
 * gate that rejects bad shapes.
 */
const MEMORY_RE = /^\d{1,6}[mMgG][iI]?$/;

export function normalizeResources(resources: unknown): {
  cpu: number | null;
  ram_mb: number | null;
} {
  let cpu: number | null = null;
  let ram_mb: number | null = null;
  if (resources && typeof resources === 'object' && !Array.isArray(resources)) {
    const r = resources as Record<string, unknown>;
    const rawCpu = r.cpu;
    if (typeof rawCpu === 'number' && Number.isFinite(rawCpu) && rawCpu > 0) {
      cpu = rawCpu;
    }
    const rawMem = r.memory;
    if (typeof rawMem === 'string' && MEMORY_RE.test(rawMem)) {
      const spec = /[iI]$/.test(rawMem) ? rawMem.slice(0, -1) : rawMem;
      const amount = Number(spec.slice(0, -1));
      if (Number.isFinite(amount)) {
        ram_mb = spec.slice(-1).toLowerCase() === 'g' ? amount * 1024 : amount;
      }
    }
  }
  return { cpu, ram_mb };
}

/**
 * Sum of persistent reservations on a host: reserved_cpu / reserved_ram_mb
 * over deployments in resource-consuming states. Called with the candidate
 * host row already locked FOR UPDATE inside the caller's transaction, so
 * the read is serialized against concurrent schedulers.
 */
export async function reservedOnHost(
  client: PoolClient,
  hostId: string,
): Promise<{ cpu: number; ram_mb: number }> {
  const { rows } = await client.query(
    `SELECT COALESCE(SUM(reserved_cpu), 0) AS cpu,
            COALESCE(SUM(reserved_ram_mb), 0) AS ram_mb
     FROM deployments
     WHERE host_id = $1 AND status = ANY($2)`,
    [hostId, [...CONSUMING_DEPLOYMENT_STATUSES]],
  );
  const row = rows[0] ?? {};
  return { cpu: numOrNull(row.cpu) ?? 0, ram_mb: numOrNull(row.ram_mb) ?? 0 };
}

/**
 * Capacity check against persistent reservations: the deployment fits when
 * (host total − already reserved) covers the requirement on both
 * dimensions. A requirement with an unknown/non-positive host total fails
 * closed — it cannot be verified, so the host is ineligible.
 */
export function hostHasCapacity(
  totalCpu: unknown,
  totalRamMb: unknown,
  reserved: { cpu: number; ram_mb: number },
  requiredCpuCores: number | null,
  requiredRamMb: number | null,
): boolean {
  if (requiredCpuCores !== null && requiredCpuCores !== undefined) {
    const t = numOrNull(totalCpu);
    if (t === null || t <= 0) return false;
    if (t - reserved.cpu < requiredCpuCores) return false;
  }
  if (requiredRamMb !== null && requiredRamMb !== undefined) {
    const t = numOrNull(totalRamMb);
    if (t === null || t <= 0) return false;
    if (t - reserved.ram_mb < requiredRamMb) return false;
  }
  return true;
}

function hostMeetsConstraints(host: CandidateHost, c: HostSelectionConstraints): boolean {
  const caps = Array.isArray(host.capabilities)
    ? (host.capabilities as unknown[]).filter((x): x is string => typeof x === 'string')
    : [];
  for (const req of c.requiredCapabilities) {
    if (!caps.includes(req)) return false;
  }
  return true;
}

/**
 * Capacity check for one already-locked host row: compute its persistent
 * reservations and verify both dimensions fit. Shared by the auto-select
 * path and the explicit host_id path so both reserve atomically.
 */
export async function checkHostCapacity(
  client: PoolClient,
  host: { id: string; total_cpu?: unknown; total_ram_mb?: unknown },
  requiredCpuCores: number | null,
  requiredRamMb: number | null,
): Promise<boolean> {
  const reserved = await reservedOnHost(client, host.id);
  return hostHasCapacity(host.total_cpu, host.total_ram_mb, reserved, requiredCpuCores, requiredRamMb);
}

/**
 * Does the agent's host-targeting ACL allow this host? Legacy-open when the
 * host carries no agent_host_access rows at all; strict otherwise.
 */
export async function hostAclAllows(
  client: PoolClient,
  agentId: string,
  hostId: string,
): Promise<boolean> {
  const { rows } = await client.query(
    'SELECT agent_id FROM agent_host_access WHERE host_id = $1',
    [hostId],
  );
  if (rows.length === 0) return true; // legacy-open
  return rows.some((r) => r.agent_id === agentId);
}

/**
 * Pick an eligible host for a deployment. Returns the host id, or null when
 * no host is eligible.
 *
 * MUST be called inside the caller's transaction (the deployment INSERT
 * follows in the same transaction): the FOR UPDATE lock on the candidate
 * rows makes check-and-reserve atomic per §9 — the lock is held while
 * draining state, capabilities, persistent reservations, and the host ACL
 * are evaluated, so two concurrent requests cannot select on the same
 * stale snapshot. On real PostgreSQL the second request blocks on the row
 * lock until the first commits, then re-reads reservations that include
 * the first request's deployment row.
 */
export async function selectHostForDeployment(
  client: PoolClient,
  constraints: HostSelectionConstraints,
): Promise<string | null> {
  const { rows } = await client.query(
    `SELECT h.id, h.status, h.worker_draining, h.capabilities, h.total_cpu, h.total_ram_mb
     FROM hosts h
     WHERE h.status = 'online'
     ORDER BY h.created_at ASC
     FOR UPDATE`,
  );
  for (const host of rows as CandidateHost[]) {
    // Part 1F/1G: draining exclusion — operator-set status OR the worker's
    // self-reported drain flag. Draining stops new scheduling only; it
    // never touches existing apps or owned tasks.
    if (!isHostSchedulable(host)) continue;
    if (!hostMeetsConstraints(host, constraints)) continue;
    // Part 1D: capacity from PERSISTENT reservations, computed while the
    // host row is locked, inside the caller's transaction.
    if (
      !(await checkHostCapacity(
        client,
        { id: host.id as string, total_cpu: host.total_cpu, total_ram_mb: host.total_ram_mb },
        constraints.requiredCpuCores,
        constraints.requiredRamMb,
      ))
    ) {
      continue;
    }
    // The host row stays locked for the rest of the transaction while the
    // ACL is evaluated, so the check and the later reservation are atomic.
    if (!(await hostAclAllows(client, constraints.agentId, host.id as string))) continue;
    logger.info('host selected for deployment', { host: host.id });
    return host.id as string;
  }
  logger.info('no eligible host for deployment', {
    requiredCapabilities: constraints.requiredCapabilities,
  });
  return null;
}

export interface ProjectDeploymentConstraints {
  requiredCapabilities: string[];
  requiredCpuCores: number | null;
  requiredRamMb: number | null;
}

/**
 * Derive scheduling constraints from an agent.deploy.json manifest shape:
 * runtime -> required host capability, plus resources.cpu /
 * resources.memory when the manifest declares them, via the single
 * authoritative parser (Part 1B).
 *
 * Fix #1: the manifest passed here must be the artifact's validated
 * manifest when the deployment references one — the exact definition the
 * worker will deploy. Callers fall back to the project's configuration
 * only when no artifact manifest exists (legacy artifacts).
 */
export function deploymentConstraintsForManifest(
  manifest: unknown,
  fallbackRuntime?: unknown,
): ProjectDeploymentConstraints {
  const cfg = (manifest ?? {}) as Record<string, unknown>;
  const runtime =
    typeof cfg.runtime === 'string' ? cfg.runtime : typeof fallbackRuntime === 'string' ? fallbackRuntime : 'docker';
  const runtimeCaps =
    runtime === 'docker-compose' ? ['docker-compose'] : runtime === 'docker' ? ['docker'] : [];
  const extraCaps = Array.isArray(cfg.capabilities)
    ? (cfg.capabilities as unknown[]).filter((c): c is string => typeof c === 'string')
    : [];
  const requiredCapabilities = [...new Set([...runtimeCaps, ...extraCaps])];
  const { cpu, ram_mb } = normalizeResources(cfg.resources);
  return { requiredCapabilities, requiredCpuCores: cpu, requiredRamMb: ram_mb };
}

/**
 * Legacy entry point: constraints from a project's stored configuration.
 * Prefer deploymentConstraintsForManifest with the artifact's validated
 * manifest (Fix #1); this remains for the no-artifact/legacy path.
 */
export function deploymentConstraintsForProject(project: {
  runtime?: unknown;
  configuration?: unknown;
}): ProjectDeploymentConstraints {
  return deploymentConstraintsForManifest(project.configuration, project.runtime);
}
