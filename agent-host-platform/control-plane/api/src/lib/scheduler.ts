// Host selection for deployments with an omitted host_id (§8) and the
// atomic check-and-reserve contract (§9).
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
// plus the port_allocations PK insert for an explicit host_port) commit
// atomically. PostgreSQL row locks are the serialization mechanism — there
// is no in-memory locking anywhere in this path. Concurrent deployment
// requests serialize on the host row instead of racing on stale
// utilization snapshots.

import type { PoolClient } from 'pg';
import { logger } from './log';

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
  capabilities: unknown;
  total_cpu: number | null;
  total_ram_mb: number | null;
  cpu_pct: number | null;
  ram_pct: number | null;
}

function availableFraction(host: CandidateHost, total: number | null, pct: number | null): number | null {
  const t = Number(total);
  if (!Number.isFinite(t) || t <= 0) return null; // cannot verify
  const p = Number(pct);
  const used = Number.isFinite(p) && p >= 0 ? Math.min(p, 100) : 0;
  return t * (1 - used / 100);
}

function hostMeetsConstraints(host: CandidateHost, c: HostSelectionConstraints): boolean {
  const caps = Array.isArray(host.capabilities)
    ? (host.capabilities as unknown[]).filter((x): x is string => typeof x === 'string')
    : [];
  for (const req of c.requiredCapabilities) {
    if (!caps.includes(req)) return false;
  }
  if (c.requiredCpuCores !== null && c.requiredCpuCores !== undefined) {
    const avail = availableFraction(host, host.total_cpu, host.cpu_pct);
    if (avail === null || avail < c.requiredCpuCores) return false;
  }
  if (c.requiredRamMb !== null && c.requiredRamMb !== undefined) {
    const avail = availableFraction(host, host.total_ram_mb, host.ram_pct);
    if (avail === null || avail < c.requiredRamMb) return false;
  }
  return true;
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
 * capabilities, resources, and the host ACL are evaluated, so two
 * concurrent requests cannot select on the same stale snapshot.
 */
export async function selectHostForDeployment(
  client: PoolClient,
  constraints: HostSelectionConstraints,
): Promise<string | null> {
  const { rows } = await client.query(
    `SELECT h.id, h.capabilities, h.total_cpu, h.total_ram_mb, h.cpu_pct, h.ram_pct
     FROM hosts h
     WHERE h.status = 'online'
     ORDER BY h.created_at ASC
     FOR UPDATE`,
  );
  for (const host of rows as CandidateHost[]) {
    if (!hostMeetsConstraints(host, constraints)) continue;
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
 * Derive scheduling constraints from a project's validated configuration
 * (agent.deploy.json shape): runtime -> required host capability, plus
 * resources.cpu / resources.memory when the manifest declares them.
 */
export function deploymentConstraintsForProject(project: {
  runtime?: unknown;
  configuration?: unknown;
}): ProjectDeploymentConstraints {
  const cfg = (project.configuration ?? {}) as Record<string, unknown>;
  const runtime = typeof project.runtime === 'string' ? project.runtime : 'docker';
  const runtimeCaps =
    runtime === 'docker-compose' ? ['docker-compose'] : runtime === 'docker' ? ['docker'] : [];
  const extraCaps = Array.isArray(cfg.capabilities)
    ? (cfg.capabilities as unknown[]).filter((c): c is string => typeof c === 'string')
    : [];
  const requiredCapabilities = [...new Set([...runtimeCaps, ...extraCaps])];
  let requiredCpuCores: number | null = null;
  let requiredRamMb: number | null = null;
  const resources = cfg.resources;
  if (resources && typeof resources === 'object' && !Array.isArray(resources)) {
    const r = resources as Record<string, unknown>;
    if (typeof r.cpu === 'number' && r.cpu > 0) requiredCpuCores = r.cpu;
    if (typeof r.memory === 'string') {
      const m = /^(\d+)(m|g)$/i.exec(r.memory.trim());
      if (m) {
        requiredRamMb = m[2].toLowerCase() === 'g' ? Number(m[1]) * 1024 : Number(m[1]);
      }
    }
  }
  return { requiredCapabilities, requiredCpuCores, requiredRamMb };
}
