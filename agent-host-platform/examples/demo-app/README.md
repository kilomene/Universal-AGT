# demo-app (example)

The smallest possible Universal-AGT deployable: a **zero-dependency**
Python (stdlib only) web app with two routes.

| Method | Path      | Response                          |
|--------|-----------|-----------------------------------|
| `GET`  | `/`       | `200` hello page (HTML)           |
| `GET`  | `/health` | `200 { "ok": true }`              |
| `GET`  | `/version`| `200 { "name", "version" }`       |

## Run locally

```bash
python3 server.py                 # listens on $PORT or 3000
curl -s localhost:3000/health    # {"ok": true}
curl -s localhost:3000/          # hello page
```

## Deploy via Universal-AGT

```bash
# from the repo root
tar -czf /tmp/demo-app.tar.gz -C agent-host-platform/examples/demo-app .
# then: artifact init → upload → POST /v1/deployments (see docs/agent-guide.md)
# the worker builds the Dockerfile, runs it, and health-checks GET /health
# before marking the deployment `running`.
```

The manifest (`agent.deploy.json`) requests 64 MB RAM / 0.1 CPU and
`restart: unless-stopped`. `HEALTH_FAIL=1` in the environment makes
`/health` return 500 — used by the E2E suite's health-fail-rollback
scenario, never in real use.
