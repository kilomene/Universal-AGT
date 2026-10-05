# agents (module map)

Implemented in `api/src/routes/agents.ts` + the `agents` table
(`database/schema/schema.sql`).

- Agent-neutral: every agent (Muse, Instinct, custom, CLI) is just a row with
  identity, capabilities, and scoped permissions. Nothing is special-cased.
- `POST /v1/agents/register` issues the API key (shown once).
- See `agent-sdk/protocol/PROTOCOL.md` §3.1.
