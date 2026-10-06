-- 009_heartbeat_enrichment.sql
--
-- WS-B follow-up (§37): the host worker reports drain/identity fields in
-- its heartbeat payload (host-worker/agent/main.py enrich_heartbeat_payload)
-- that the control plane previously dropped on the floor:
--
--   * worker_draining  — the worker's self-reported drain flag (it has
--    paused its own claim loop). The task-claim gate refuses new claims
--    while this is set, as defense in depth for a worker that reports
--    draining but still calls claim.
--   * worker_status    — "running" | "draining", as reported.
--   * ingress          — JSONB descriptor {enabled, provider} for the
--    host's ingress configuration.
--   * reported_host_name — the worker's configured host name (kept
--    separate from hosts.name, which is the registration identity).
--
-- capabilities already had a column; the heartbeat UPDATE now refreshes
-- it from the payload as well.

ALTER TABLE hosts ADD COLUMN IF NOT EXISTS worker_draining boolean NOT NULL DEFAULT false;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS worker_status text;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ingress jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS reported_host_name text;
