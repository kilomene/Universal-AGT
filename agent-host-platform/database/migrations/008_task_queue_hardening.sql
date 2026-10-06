-- 008_task_queue_hardening.sql
--
-- WS-D production hardening (§5 task queue, §6 leases, §7 idempotency,
-- §49 DB contract, §73 indexes, §74 concurrency).
--
--  * domains.status: the application code (routes/domains.ts) has a full
--    'degraded' lifecycle (active -> degraded -> configuring -> active,
--    driven by the domain reconciler's verifyTunnelRoutes), but the CHECK
--    constraint from 007 never allowed it — the first degrade attempt died
--    with a check violation. The constraint is dropped and re-added with
--    'degraded' included. (PostgreSQL cannot ALTER a CHECK expression in
--    place; domains_status_check is the deterministic {table}_{column}_check
--    name for the inline constraint from 007.)
--  * domains.idempotency_key: unique, for idempotent domain creation
--    (PROTOCOL §2, same pattern as tasks/deployments). NULLs are
--    unaffected (PostgreSQL treats them as distinct).
--  * tasks.type: DB-level check over the 18 PROTOCOL §3.2 task types — the
--    API validates these, but the invariant must hold even under
--    concurrent API requests (§49).
--  * tasks.attempts >= 0, tasks.max_attempts >= 1: the retry budget
--    bookkeeping (claim increments, sweeper increments) must never go
--    negative or zero-out the budget.
--  * deployments.health_status: constrained to the values the control
--    plane actually writes ('healthy' | 'unhealthy' | 'unknown').
--  * Indexes matched to hot query patterns (§73):
--      - idx_tasks_claimable: the atomic-claim subquery and the heartbeat
--        pending_tasks count both scan status='queued' ordered by
--        (priority DESC, created_at ASC).
--      - idx_tasks_claimed_by: worker-ownership lookups (progress auth,
--        secrets/download scoping, sweeper reporting).
--      - idx_tasks_payload_deployment_id: latestTaskForDeployment
--        (GET /v1/deployments/:id and the deployment idempotency replay
--        path) filters on payload->>'deployment_id'.
--
-- Runs in the migration runner's per-file transaction; every statement is
-- re-runnable via IF NOT EXISTS where PostgreSQL supports it.

-- -- domains.status: allow 'degraded' ---------------------------------------
ALTER TABLE domains DROP CONSTRAINT IF EXISTS domains_status_check;
ALTER TABLE domains ADD CONSTRAINT domains_status_check CHECK (
  status IN ('requested', 'configuring', 'active', 'degraded',
             'failed', 'removing', 'removed')
);

-- -- domains.idempotency_key (§7) --------------------------------------------
ALTER TABLE domains ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE domains ADD CONSTRAINT domains_idempotency_key_key UNIQUE (idempotency_key);

-- -- tasks invariants (§5/§49) -------------------------------------------------
ALTER TABLE tasks ADD CONSTRAINT chk_tasks_type CHECK (
  type IN ('deploy', 'restart', 'stop', 'start', 'remove', 'rollback',
           'logs', 'status', 'healthcheck', 'build', 'docker-build',
           'docker-run', 'docker-compose', 'environment-update',
           'artifact-download', 'artifact-upload', 'system-info',
           'ingress-sync')
);
ALTER TABLE tasks ADD CONSTRAINT chk_tasks_attempts CHECK (attempts >= 0);
ALTER TABLE tasks ADD CONSTRAINT chk_tasks_max_attempts CHECK (max_attempts >= 1);

-- -- deployments.health_status (§49) ------------------------------------------
ALTER TABLE deployments ADD CONSTRAINT chk_deployments_health_status CHECK (
  health_status IS NULL OR health_status IN ('healthy', 'unhealthy', 'unknown')
);

-- -- hot-path indexes (§73) ------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tasks_claimable
  ON tasks (priority DESC, created_at ASC)
  WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_tasks_claimed_by
  ON tasks (claimed_by)
  WHERE claimed_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_payload_deployment_id
  ON tasks ((payload ->> 'deployment_id'));
