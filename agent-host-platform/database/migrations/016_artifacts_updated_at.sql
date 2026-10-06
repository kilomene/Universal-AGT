-- ============================================================================
-- 016_artifacts_updated_at.sql
--
-- Bug found by real-PostgreSQL validation (2026-10-06): migration 001
-- installs a `touch_updated_at()` trigger on every table in
-- ('agents','hosts','tasks','projects','artifacts','deployments',
-- 'secrets'), but the `artifacts` table was created WITHOUT an
-- `updated_at` column. Any UPDATE of an artifact row (e.g. the
-- upload-finalization `UPDATE artifacts SET status = 'ready'`) fails on
-- real PostgreSQL with `record "new" has no field "updated_at"`.
-- pg-mem does not execute the trigger body the same way, so the test
-- suite never caught it.
--
-- Fix: add the missing column, matching every other table in the
-- trigger list. Owner-role-safe DDL (plain ADD COLUMN with a default;
-- no privilege issues).
-- ============================================================================

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
