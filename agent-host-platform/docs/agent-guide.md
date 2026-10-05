# Agent guide — deploying through Universal AGT

This guide is for any agent (LLM-driven, scripted, or a human using the
`agent-host` CLI). The flow is the same in every SDK because every SDK implements
[`../agent-sdk/protocol/PROTOCOL.md`](../agent-sdk/protocol/PROTOCOL.md):
**register → create project → upload artifact → deploy → poll status**.

You never need to stay online during a deploy. The task queue is durable:
create the task, disconnect, come back later, read the result.

## 1. Register and get an API key

One-time. The key is shown **once** — store it in your agent's secret
storage (never in code, tickets, or chat logs). If the control plane has
`UAHT_PROVISIONING_TOKEN` set (production always does), you must also send
header `X-Provisioning-Token: <the operator's token>` — get the value from
whoever runs the control plane; without it registration 403s after the
bootstrap registration.

```bash
export UAGT_CP="https://control-plane.example.com"

curl -s -X POST $UAGT_CP/v1/agents/register \
  -H 'Content-Type: application/json' \
  -H "X-Provisioning-Token: <operator-provided>" \
  -d '{"name": "my-builder-agent", "type": "ci",
       "capabilities": ["docker"],
       "permissions": {"deploy": true, "read_status": true,
                       "read_logs": true, "restart": true,
                       "approve_deployments": false}}' | tee agent.json
# → 201 {"agent": {...}, "api_key": "<show once>"}

export UAGT_API_KEY="<paste from agent.json>"
```

Request only the permissions you need. If you only ever deploy and read
status, don't ask for `manage_secrets` or `manage_domains`.

## 2. Create a project

```bash
curl -s -X POST $UAGT_CP/v1/projects \
  -H "Authorization: Bearer $UAGT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "url-shortener", "runtime": "docker"}' | tee project.json
# → {"project": {"id": "<uuid>", ...}}
export UAGT_PROJECT_ID="<uuid>"
```

## 3. Upload the artifact

Artifacts are content-addressed by SHA-256. Compute the checksum locally,
register the upload, then PUT the bytes. The server rejects size or
checksum mismatches, and the worker verifies the checksum **again** after
download before building.

```bash
tar -czf build.tar.gz -C my-app .
SHA=$(sha256sum build.tar.gz | cut -d' ' -f1)

INIT=$(curl -s -X POST $UAGT_CP/v1/artifacts/init \
  -H "Authorization: Bearer $UAGT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"project_id\": \"$UAGT_PROJECT_ID\", \"filename\": \"build.tar.gz\",
       \"size\": $(stat -c%s build.tar.gz),
       \"checksum\": \"sha256:$SHA\", \"version\": \"1.0.0\"}")
ARTIFACT_ID=$(echo "$INIT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["artifact"]["id"])')
UPLOAD_URL=$(echo "$INIT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["upload_url"])')

curl -s -X PUT "$UAGT_CP$UPLOAD_URL" \
  -H "Authorization: Bearer $UAGT_API_KEY" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @build.tar.gz
# → 200/201 on success; 422 if checksum/size mismatch
```

Your artifact root should contain an `agent.deploy.json` manifest
(PROTOCOL §4) — the worker validates it before any build, and an invalid
manifest fails the task fast with no partial state. See
[`../examples/`](../examples/) for working examples.

## 4. Deploy

```bash
IDEMPOTENCY_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')

curl -s -X POST $UAGT_CP/v1/deployments \
  -H "Authorization: Bearer $UAGT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{
    \"project_id\": \"$UAGT_PROJECT_ID\",
    \"version\": \"1.0.0\",
    \"artifact_id\": \"$ARTIFACT_ID\",
    \"mode\": \"automatic\",
    \"idempotency_key\": \"$IDEMPOTENCY_KEY\"
  }" | tee deployment.json
# → 201 {"deployment": {"id": "...", "status": "requested", ...},
#        "task": {"id": "...", "status": "queued", ...}}
```

**Always send an `idempotency_key`.** If your request times out, re-send the
*identical* body: you'll get HTTP 200 with `"idempotent_replay": true`
instead of a second deployment. Same key + different body → HTTP 409.

You can target a host with `"host_id"`, or omit it and let any capable
host claim the task.

### Automatic vs manual approval mode

- `"mode": "automatic"` (default): the task goes `queued → claimed →
  running → completed|failed` with no human in the loop.
- `"mode": "manual"`: the task is **created** in `awaiting_approval` — no
  worker can claim it until then. An approver (an agent/human with the
  `approve_deployments` permission) runs:

```bash
curl -s -X POST $UAGT_CP/v1/tasks/<task-id>/approve \
  -H "Authorization: Bearer $APPROVER_KEY"
# or /reject to cancel it
```

The dashboard shows `awaiting_approval` tasks in purple; the event stream
emits `task.awaiting_approval`.

## 5. Check status (now or much later)

```bash
DEPLOYMENT_ID=$(python3 -c 'import json; print(json.load(open("deployment.json"))["deployment"]["id"])')

curl -s $UAGT_CP/v1/deployments/$DEPLOYMENT_ID \
  -H "Authorization: Bearer $UAGT_API_KEY" | python3 -m json.tool
# deployment.status: requested|building|starting|healthcheck|running|...
# deployment.health_status: healthy|...
# deployment.ports / .domains tell you where it is reachable
```

Task-level detail (logs included) lives on the task row:

```bash
TASK_ID=$(python3 -c 'import json; print(json.load(open("deployment.json"))["task"]["id"])')
curl -s "$UAGT_CP/v1/tasks/$TASK_ID" -H "Authorization: Bearer $UAGT_API_KEY"
# {"task": {"status": "completed", "result": {...}, "error": null, ...}}
```

Or watch everything live: open the dashboard (served at `/` by the control
plane) or tail the SSE stream:

```bash
curl -N -H "Authorization: Bearer $UAGT_API_KEY" $UAGT_CP/v1/events/stream
```

## 6. Day-two operations

```bash
# service id == deployment id in the friendly services view
curl -s $UAGT_CP/v1/services -H "Authorization: Bearer $UAGT_API_KEY"

curl -s -X POST $UAGT_CP/v1/services/<id>/restart -H "Authorization: Bearer $UAGT_API_KEY"
curl -s -X POST $UAGT_CP/v1/services/<id>/stop    -H "Authorization: Bearer $UAGT_API_KEY"
curl -s -X POST $UAGT_CP/v1/services/<id>/start   -H "Authorization: Bearer $UAGT_API_KEY"

# roll back to the previous healthy version
curl -s -X POST $UAGT_CP/v1/deployments/<id>/rollback \
  -H "Authorization: Bearer $UAGT_API_KEY"

# fetch logs of a deployment (task type=logs under the hood)
curl -s -X POST $UAGT_CP/v1/tasks \
  -H "Authorization: Bearer $UAGT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"type": "logs", "payload": {"deployment_id": "<id>"}}'
```

## 7. The CLI shortcut

The `agent-host` CLI (`cli/` in this repo) wraps the read and operate
calls. Install it with `pip install -e ../agent-sdk/python` then
`pip install -e .` from `cli/`; configure with `UAHT_BASE_URL` and
`UAHT_API_KEY` (or `--base-url` / `--api-key` flags):

```bash
export UAHT_BASE_URL="https://control-plane.example.com"
export UAHT_API_KEY="<your agent api key>"

agent-host hosts                                # list persistent hosts
agent-host apps                                 # list running services
agent-host deploy --project url-shortener --version 1.0.0 \
  --artifact ./build.tar.gz                     # upload + deploy in one step
agent-host deploy --project url-shortener --version 1.0.0 \
  --host persistent-host-01 --mode manual       # manual approval mode
agent-host status --deployment <id>             # deployment status
agent-host logs --deployment <id> --follow      # stream logs
agent-host restart --deployment <id>
agent-host rollback --deployment <id>           # previous healthy version
agent-host tasks --status running               # durable queue
agent-host events --follow                      # live SSE event stream
```

Append `--json` to any command for machine-readable output. Registration,
project creation, and artifact uploads outside `deploy --artifact` go
through the REST calls in §§1–3 above.

## Rules of the road for agents

1. **Idempotency keys on every mutating call.** Retries are safe; duplicates
   are not.
2. **Don't poll aggressively.** The task row keeps your result; poll with
   backoff or watch the SSE stream / dashboard.
3. **Least privilege.** Register with the minimal permission set for the
   job. Secret management belongs to a separate, tightly-scoped agent.
4. **Never exfiltrate tokens.** API keys and host tokens live in secret
   storage or root-only files — never in prompts, tickets, logs, or repos.
5. **Validate `agent.deploy.json` locally** (`examples/validate.py` shows
   the rules) before uploading — the worker fails invalid manifests fast,
   and a wasted build cycle is on you.
