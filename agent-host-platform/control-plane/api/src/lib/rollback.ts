// Rollback target selection — PURE functions, zero DB.
//
// POST /v1/deployments/:id/rollback does NOT create a new deployment row
// (that design INSERTed a duplicate (project_id, host_id, version) and died
// with a 500 unique violation). Instead it creates a NEW TASK
// (type=rollback, payload {deployment_id, target_deployment_id}) where the
// target is the last healthy deployment of the same project+host. The
// worker's rollback handler performs the restore and reports; the control
// plane emits `deployment.rollback_requested` on creation and
// `deployment.rolled_back` on worker completion.

export interface RollbackCandidate {
  id: string;
  project_id: string;
  host_id: string | null;
  version: string;
  status: string;
  health_status: string | null;
  created_at: Date | string;
}

export interface RollbackCurrent {
  id: string;
  project_id: string;
  host_id: string | null;
}

/**
 * Pick the rollback target: the newest deployment of the same
 * project+host (excluding the current one) that is `running` or
 * `superseded` and `healthy`. `superseded` rows are the normal case:
 * when a newer deployment became healthy, the previous one was moved
 * there (migration 015) precisely so it stays available as a rollback
 * target without holding a resource reservation. Returns null when there
 * is nothing safe to roll back to.
 */
export function selectRollbackTarget(
  current: RollbackCurrent,
  candidates: RollbackCandidate[],
): RollbackCandidate | null {
  const eligible = candidates.filter(
    (c) =>
      c.id !== current.id &&
      c.project_id === current.project_id &&
      c.host_id === current.host_id &&
      (c.status === 'running' || c.status === 'superseded') &&
      c.health_status === 'healthy',
  );
  eligible.sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  return eligible[0] ?? null;
}
