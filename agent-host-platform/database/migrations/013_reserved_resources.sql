-- ============================================================================
-- 013_reserved_resources.sql
--
-- Part 1C/1D — persistent, atomic resource reservations. The deployments
-- table previously stored no resource data at all: the scheduler could only
-- check the host's observed cpu_pct/ram_pct snapshot, so two concurrent
-- deployment requests could both pass the check against the same stale
-- snapshot and over-commit the host.
--
-- These columns snapshot the deployment's normalized resource requirement
-- (from the project's agent.deploy.json `resources`, parsed by the single
-- authoritative rule shared by the scheduler and the worker) at creation
-- time, inside the same transaction that selects the host and inserts the
-- deployment row. The reservation query sums these columns over deployments
-- in resource-consuming states; a status transition to a terminal state
-- releases the reservation automatically (the row stops matching the
-- consuming-state filter). Reservations therefore live in PostgreSQL —
-- never in process memory — and survive control-plane and worker restarts.
--
-- NULL = the deployment declares no requirement for that dimension.
-- ============================================================================

ALTER TABLE deployments ADD COLUMN IF NOT EXISTS reserved_cpu double precision;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS reserved_ram_mb double precision;
