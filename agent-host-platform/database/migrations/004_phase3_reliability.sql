-- 004: Phase 3 — task/deployment reliability.
--
--  * tasks.lease_expires_at / tasks.last_progress_at: claim leases. The
--    claim records lease_expires_at = now() + TASK_CLAIM_LEASE_S (default
--    600s); every progress report refreshes it. The stuck-task sweeper
--    requeues tasks stuck in claimed/running past their lease.
--  * deployments.mode / deployments.artifact_id: deploymentIdempotencyBody
--    compares these fields, but the deployments row never had the columns,
--    so idempotent replays of manual-mode / artifact deploys compared
--    undefined-vs-undefined and produced false 409 conflicts. The columns
--    are written on creation going forward; existing rows are backfilled
--    from deployment.requested events (mode) and the linked deploy task's
--    payload (artifact_id).
--  * port_allocations: registry of fixed host ports reserved for
--    deployments. unique(host_id, port) makes double-allocation a 409.

-- -- claim leases -----------------------------------------------------------
ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS last_progress_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(lease_expires_at)
  WHERE status IN ('claimed', 'running');

-- -- deployment idempotency columns ------------------------------------------
ALTER TABLE deployments
  ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'automatic'
  CHECK (mode IN ('automatic', 'manual'));
ALTER TABLE deployments
  ADD COLUMN IF NOT EXISTS artifact_id uuid REFERENCES artifacts(id) ON DELETE SET NULL;

-- backfill mode from the deployment.requested event payload (best effort;
-- rows without the event keep the 'automatic' default)
UPDATE deployments
SET mode = COALESCE((
  SELECT e.payload->>'mode'
  FROM events e
  WHERE e.deployment_id = deployments.id
    AND e.type = 'deployment.requested'
    AND e.payload->>'mode' IN ('automatic', 'manual')
  ORDER BY e.id DESC
  LIMIT 1
), 'automatic')
WHERE mode = 'automatic';

-- backfill artifact_id from the linked deploy task payload (best effort)
UPDATE deployments
SET artifact_id = (
  SELECT (t.payload->>'artifact_id')::uuid
  FROM tasks t
  WHERE t.id = deployments.task_id
    AND t.type = 'deploy'
    AND t.payload->>'artifact_id' ~ '^[0-9a-fA-F-]{36}$'
)
WHERE artifact_id IS NULL;

-- -- port registry ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS port_allocations (
    host_id       uuid NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    port          integer NOT NULL CHECK (port > 0 AND port < 65536),
    deployment_id uuid NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    allocated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (host_id, port)
);
CREATE INDEX IF NOT EXISTS idx_port_allocations_deployment
  ON port_allocations(deployment_id);
