# url-shortener (example)

A minimal URL shortener with **zero dependencies** — Node's built-in `http`
module only. Shipped as a Docker image per its `agent.deploy.json`.

## API

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/shorten` | `{ "url": "https://example.com/..." }` | `201 { "code": "Sx0RPw", "url": "..." }` |
| `GET` | `/:code` | — | `302` redirect to the stored URL (`404` if unknown) |
| `GET` | `/health` | — | `200 { "ok": true }` |

Codes are random 6-character alphanumeric strings. Storage is in-memory,
so codes reset when the container restarts — fine for an example, not for
production.

## Run locally

```bash
node server.js                  # listens on $PORT or 3000
curl -s localhost:3000/health
curl -s -X POST localhost:3000/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/some/page"}'
```

## Deploy via Universal AGT

```bash
# from the repo root
tar -czf /tmp/url-shortener.tar.gz -C agent-host-platform/examples/url-shortener .
# then: artifact init → upload → POST /v1/deployments (see docs/agent-guide.md)
```

The worker builds the `Dockerfile`, runs the container with 256 MB RAM /
0.5 CPU, and health-checks `GET /health` before marking the deployment
`running`.
