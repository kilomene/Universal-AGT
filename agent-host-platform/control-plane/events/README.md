# events (module map)

Implemented in `api/src/lib/events.ts`, `api/src/routes/events.ts`
(incl. the SSE stream), and the append-only `events` table
(`database/schema/schema.sql` + `003_events_notify.sql`).

- INSERT-only: a database trigger rejects UPDATE/DELETE.
- Live fan-out via `pg_notify('uag_events', id)` with a poll backstop.
- `GET /v1/events` (cursor pagination) and `GET /v1/events/stream` (SSE).
- See `agent-sdk/protocol/PROTOCOL.md` §3.8.

## Visibility: global by design

The journal is **globally visible by design**: any agent with the
`read_status` permission reads the full stream — there is deliberately no
per-project / per-host / per-agent ACL filtering (see
`docs/security.md`, "Event visibility: global by design"). The
compensating invariant is absolute: event payloads must never carry
secret material (API keys, host tokens, secret values, provisioning
tokens, ciphertext). Enforced by `api/test/eventVisibility.test.ts`.
