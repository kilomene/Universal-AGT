-- ============================================================================
-- 015_superseded_status.sql
--
-- Issue #2 (superseded reservation leak): when a new deployment becomes
-- healthy, the worker stops the previous deployment's container and marks
-- it `superseded` locally — but the control plane left the previous
-- deployment row `running`, so its reserved_cpu / reserved_ram_mb kept
-- counting against the host forever. The scheduler eventually believed the
-- host was full while only the newest deployment was actually running.
--
-- The fix introduces an explicit `superseded` deployment lifecycle state:
-- the control plane transitions the previous deployment(s) of the same
-- project+host to `superseded` in the same transaction that marks the new
-- deployment `running`. `superseded` is deliberately NOT in the
-- resource-consuming states (the reservation is released) but the row is
-- preserved as a valid rollback target (rollback restores it to
-- `running`).
--
-- This migration widens the deployments status CHECK to admit the new
-- value. The original CHECK was created inline in 001_initial.sql, so
-- PostgreSQL auto-named it `deployments_status_check` (renamed by 012).
-- DROP IF EXISTS + ADD makes a re-run a safe no-op.
-- ============================================================================

ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployments_status_check;

ALTER TABLE deployments ADD CONSTRAINT deployments_status_check
  CHECK (status IN ('requested','approved','building','starting',
                    'healthcheck','running','failed','rolled_back',
                    'rollback_failed','stopped','stopping','superseded'));
