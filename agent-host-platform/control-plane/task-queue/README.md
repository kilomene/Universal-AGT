# task-queue (module map)

Implemented in `api/src/routes/tasks.ts`, `api/src/routes/worker.ts`, and
`api/src/lib/stateMachine.ts`.

- Durable queue backed by the `tasks` table; the task row is the source of truth.
- Status machine: `queued → claimed → running → completed|failed`
  (`running → awaiting_approval → queued|cancelled` in manual mode).
- Atomic claim via `SELECT ... FOR UPDATE SKIP LOCKED` (+ `?wait=` long-poll).
- Idempotency keys dedupe task creation (`idempotent_replay` on reuse).
- See `agent-sdk/protocol/PROTOCOL.md` §3.2–§3.3.
