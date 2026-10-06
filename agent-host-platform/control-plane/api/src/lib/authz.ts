// Central resource-level authorization (§10).
//
// Agent *permissions* (deploy, read_status, ...) say WHAT an agent may do.
// These helpers say WHICH resources it may do it to. Every agent route that
// touches a project, deployment, artifact, secret, host target, or task must
// go through exactly one of these helpers — no per-route authorization SQL.
//
// Ownership model (migration 011_project_ownership_acls):
//   * projects.owner_agent_id — the explicit owner agent.
//   * project_members (project_id, agent_id) — explicit ACL grants.
//   * agent_host_access (agent_id, host_id) — explicit per-host targeting grants.
//
// Backwards-compatibility rule (never break existing installations):
//   * a project with owner_agent_id IS NULL is LEGACY-OPEN: every agent may
//     access it (pre-ACL rows, and legacy rows whose owner name could not be
//     resolved — those are recorded in migration_reports, never silently
//     assigned). Assign an owner via POST /v1/projects/:id/owner to close it.
//   * a host with NO agent_host_access rows at all is LEGACY-OPEN for
//     targeting. The first explicit row for a host switches that host to
//     strict mode.
//
// Failure shape: 404 when the resource does not exist, 403 when it exists
// but the agent may not access it. (UUID secrecy is not authorization, and
// existence is not leaked through the 404/403 split beyond what the caller
// already proved by naming a real id.)

import type { Pool, PoolClient } from 'pg';
import { HttpError } from './errors';

type Db = Pool | PoolClient;

export interface ProjectAuthz {
  id: string;
  owner_agent_id: string | null;
}

/** Load a project's ownership row. 404 when the project does not exist. */
export async function getProjectAuthz(db: Db, projectId: string): Promise<ProjectAuthz> {
  const { rows } = await db.query('SELECT id, owner_agent_id FROM projects WHERE id = $1', [
    projectId,
  ]);
  if (!rows[0]) throw new HttpError(404, 'not_found', 'project not found');
  return { id: rows[0].id as string, owner_agent_id: (rows[0].owner_agent_id as string | null) ?? null };
}

/** Pure access check: does this agent have access to this project? */
export async function canAccessProject(
  db: Db,
  agentId: string,
  projectId: string,
): Promise<boolean> {
  const proj = await getProjectAuthz(db, projectId);
  if (proj.owner_agent_id === null) return true; // legacy-open (see header)
  if (proj.owner_agent_id === agentId) return true;
  const { rows } = await db.query(
    'SELECT 1 FROM project_members WHERE project_id = $1 AND agent_id = $2',
    [projectId, agentId],
  );
  return rows.length > 0;
}

/**
 * authorizeProjectAccess — the project gate. Returns the ownership row;
 * throws 404 (missing) or 403 (forbidden).
 */
export async function authorizeProjectAccess(
  db: Db,
  agentId: string,
  projectId: string,
): Promise<ProjectAuthz> {
  const proj = await getProjectAuthz(db, projectId);
  if (proj.owner_agent_id === null) return proj; // legacy-open
  if (proj.owner_agent_id === agentId) return proj;
  const { rows } = await db.query(
    'SELECT 1 FROM project_members WHERE project_id = $1 AND agent_id = $2',
    [projectId, agentId],
  );
  if (rows.length > 0) return proj;
  throw new HttpError(403, 'forbidden', 'agent does not have access to this project');
}

/**
 * Every project id the agent may access — for filtering list endpoints.
 * Includes legacy-open (ownerless) projects.
 */
export async function listAccessibleProjectIds(db: Db, agentId: string): Promise<string[]> {
  const { rows } = await db.query(
    `SELECT id FROM projects
     WHERE owner_agent_id IS NULL
        OR owner_agent_id = $1
        OR id IN (SELECT project_id FROM project_members WHERE agent_id = $1)`,
    [agentId],
  );
  return rows.map((r) => r.id as string);
}

/**
 * Builds a `column IN ($n, ...)` predicate for a list of ids, appending one
 * bind parameter per id. Returns null when ids is empty (the caller then
 * short-circuits to an empty result — `IN ()` is not valid SQL).
 *
 * Note: `= ANY($1)` with a bound array is NOT used here: it is correct on
 * real PostgreSQL but pg-mem (used by the API test-suite) mishandles the
 * column-on-the-left form. The IN-expansion is equivalent on both.
 */
export function inList(column: string, ids: string[], params: unknown[]): string | null {
  if (ids.length === 0) return null;
  const placeholders = ids.map((id) => {
    params.push(id);
    return `$${params.length}`;
  });
  return `${column} IN (${placeholders.join(', ')})`;
}

export interface DeploymentAuthz {
  id: string;
  project_id: string;
  host_id: string | null;
}

/** authorizeDeploymentAccess — via deployment -> project -> owner/ACL. */
export async function authorizeDeploymentAccess(
  db: Db,
  agentId: string,
  deploymentId: string,
): Promise<DeploymentAuthz> {
  const { rows } = await db.query(
    'SELECT id, project_id, host_id FROM deployments WHERE id = $1',
    [deploymentId],
  );
  if (!rows[0]) throw new HttpError(404, 'not_found', 'deployment not found');
  await authorizeProjectAccess(db, agentId, rows[0].project_id as string);
  return {
    id: rows[0].id as string,
    project_id: rows[0].project_id as string,
    host_id: (rows[0].host_id as string | null) ?? null,
  };
}

export interface ArtifactAuthz {
  id: string;
  project_id: string;
}

/** authorizeArtifactAccess — via artifact -> project -> project ACL. */
export async function authorizeArtifactAccess(
  db: Db,
  agentId: string,
  artifactId: string,
): Promise<ArtifactAuthz> {
  const { rows } = await db.query('SELECT id, project_id FROM artifacts WHERE id = $1', [
    artifactId,
  ]);
  if (!rows[0]) throw new HttpError(404, 'not_found', 'artifact not found');
  await authorizeProjectAccess(db, agentId, rows[0].project_id as string);
  return { id: rows[0].id as string, project_id: rows[0].project_id as string };
}

/** Pure task access check: creator, or via task -> deployment -> project. */
export async function canAccessTask(
  db: Db,
  agentId: string,
  taskId: string,
): Promise<boolean> {
  const { rows } = await db.query('SELECT id, created_by, payload FROM tasks WHERE id = $1', [
    taskId,
  ]);
  const task = rows[0];
  if (!task) return false;
  if (task.created_by !== null && task.created_by !== undefined && task.created_by === agentId) {
    return true;
  }
  const deploymentId = (task.payload as Record<string, unknown> | null)?.deployment_id;
  if (typeof deploymentId === 'string' && deploymentId) {
    try {
      await authorizeDeploymentAccess(db, agentId, deploymentId);
      return true;
    } catch {
      return false;
    }
  }
  return false;
}

/**
 * authorizeTaskAccess — via task.created_by and/or task -> deployment ->
 * project. Throws 404 (missing) or 403 (forbidden).
 */
export async function authorizeTaskAccess(
  db: Db,
  agentId: string,
  taskId: string,
): Promise<Record<string, unknown>> {
  const { rows } = await db.query('SELECT * FROM tasks WHERE id = $1', [taskId]);
  const task = rows[0] as Record<string, unknown> | undefined;
  if (!task) throw new HttpError(404, 'not_found', 'task not found');
  if (await canAccessTask(db, agentId, taskId)) return task;
  throw new HttpError(403, 'forbidden', 'agent does not have access to this task');
}

/**
 * authorizeHostAccess — may this agent explicitly target this host?
 * 404 when the host does not exist. Legacy-open when the host carries no
 * explicit agent_host_access rows at all; strict (403) otherwise.
 */
export async function authorizeHostAccess(
  db: Db,
  agentId: string,
  hostId: string,
): Promise<void> {
  const { rows } = await db.query('SELECT id FROM hosts WHERE id = $1', [hostId]);
  if (!rows[0]) throw new HttpError(404, 'not_found', 'host not found');
  const acl = await db.query('SELECT agent_id FROM agent_host_access WHERE host_id = $1', [
    hostId,
  ]);
  if (acl.rows.length === 0) return; // legacy-open (see header)
  if (acl.rows.some((r) => r.agent_id === agentId)) return;
  throw new HttpError(403, 'forbidden', 'agent does not have access to target this host');
}
