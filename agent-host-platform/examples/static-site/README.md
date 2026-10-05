# static-site (example)

A single static HTML page served by `nginx:alpine`. The simplest possible
Universal AGT deployment: no app server, no dependencies.

## Contents

- `index.html` — the page itself
- `Dockerfile` — copies it into `nginx:alpine`, exposes port 80
- `agent.deploy.json` — the deploy manifest the worker validates (PROTOCOL §4)

The worker health-checks `GET /` — nginx answers `200` for the static file,
so the deployment is marked `running` as soon as the container serves.

## Preview locally

```bash
docker build -t static-site-example .
docker run --rm -p 8080:80 static-site-example
# open http://localhost:8080
```

## Deploy via Universal AGT

```bash
tar -czf /tmp/static-site.tar.gz -C agent-host-platform/examples/static-site .
# then: artifact init → upload → POST /v1/deployments (see docs/agent-guide.md)
```
