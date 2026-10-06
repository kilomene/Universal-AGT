-- ============================================================================
-- 012_rollback_failed_status.sql
--
-- §6 rollback failure durability: the worker can report a rollback task as
-- task-status 'completed' while the rollback itself failed
-- (result.status === 'rollback_failed'). The control plane persists that
-- honestly as deployments.status = 'rollback_failed' instead of lying with
-- 'rolled_back'. This migration widens the deployments status CHECK to
-- admit the new value.
--
-- The original CHECK was created inline in 001_initial.sql, so PostgreSQL
-- auto-named it `deployments_status_check`. DROP IF EXISTS + ADD makes a
-- re-run a safe no-op.
-- ============================================================================

ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployments_status_check;

ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
  CHECK (status IN ('requested','approved','building','starting',
                    'healthcheck','running','failed','rolled_back',
                    'rollback_failed','stopped','stopping'));
