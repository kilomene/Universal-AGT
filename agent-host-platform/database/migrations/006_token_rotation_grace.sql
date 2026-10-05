-- 006_token_rotation_grace.sql
--
-- Host token rotation safe sequence (W12 auth hardening): POST
-- /v1/hosts/:id/rotate-token accepts {"grace_seconds": N} so a live worker
-- can fetch the new token, verify it authenticates, and adopt it while the
-- OLD token stays valid during a short grace window (instead of being
-- disconnected the instant it rotates). The grace columns default to NULL
-- (immediate invalidation), so existing deployments are unaffected.
--
-- attachAuth honors previous_token_hash only while
-- previous_token_expires_at is in the future.

alter table hosts
    add column if not exists previous_token_hash text,
    add column if not exists previous_token_expires_at timestamptz;
