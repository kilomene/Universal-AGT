# artifacts (module map)

Implemented in `api/src/routes/artifacts.ts` (control plane) and
`host-worker/deployments/pipeline.py` (worker side).

- `POST /v1/artifacts/init` → streaming `PUT .../content` upload.
- Server verifies byte size + SHA-256; mismatch → HTTP 422, artifact marked failed.
- Worker re-verifies SHA-256 after download before any build step.
- See `agent-sdk/protocol/PROTOCOL.md` §3.5.
