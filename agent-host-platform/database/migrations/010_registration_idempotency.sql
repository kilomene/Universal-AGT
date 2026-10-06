-- 010_registration_idempotency.sql
--
-- Registration idempotency (§7): POST /v1/hosts/register and
-- POST /v1/agents/register accept an optional idempotency_key. A retried
-- registration with the same key and equal parameters replays (the server
-- rotates to a fresh credential and returns it, since only the hash of the
-- original is stored); the same key with different parameters is a 409
-- conflict. Without a key, behavior is unchanged (duplicate name → 409).

alter table hosts
  add column if not exists idempotency_key text;

alter table agents
  add column if not exists idempotency_key text;

-- Partial unique indexes: many NULLs allowed, non-null keys unique.
create unique index if not exists idx_hosts_idempotency_key
  on hosts (idempotency_key) where idempotency_key is not null;

create unique index if not exists idx_agents_idempotency_key
  on agents (idempotency_key) where idempotency_key is not null;
