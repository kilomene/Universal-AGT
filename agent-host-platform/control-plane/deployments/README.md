# deployments (module map)

Implemented in `api/src/routes/deployments.ts` + `api/src/routes/services.ts`
(control plane) and `host-worker/deployments/` (execution).

- `POST /v1/deployments` creates a deployment row + a `deploy` task atomically.
- Worker executes: download → SHA-256 verify → manifest validate → build →
  run → healthcheck → rollback on failure.
- `POST /v1/deployments/:id/rollback` redeploys the previous healthy version.
- See `agent-sdk/protocol/PROTOCOL.md` §3.6.
