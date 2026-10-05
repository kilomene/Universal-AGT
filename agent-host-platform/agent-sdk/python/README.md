# uaht-sdk

Python (>= 3.10) SDK for the Universal Agent-to-Persistent-Host Deployment
System control plane — wire protocol v1. Depends only on `requests`.

## Install

```sh
pip install -e .
```

## Usage

```python
import os
from uaht_sdk import UahtClient, UahtError

client = UahtClient(
    base_url=os.environ["UAHT_BASE_URL"],   # e.g. https://cp.example.com
    api_key=os.environ["UAHT_API_KEY"],     # your agent API key
)

try:
    print(client.me()["agent"]["name"])
except UahtError as err:
    print(f"control plane error [{err.code}]: {err.message}")
```

## Deploy helper

```python
# Upload the artifact first, then deploy.
info = client.init_artifact("proj-uuid", "./dist/app.tar.gz", version="1.2.0")
client.upload_artifact(info["upload_url"], "./dist/app.tar.gz")

deployment, task = client.deploy(
    project="my-api",          # project name (or id)
    version="1.2.0",
    host="host-01",            # host name (or id); omit for any capable host
    artifact_id=info["artifact"]["id"],
    mode="automatic",
    wait=True,                 # poll until the task completes
)
print(deployment["status"], task["status"])
```

## Secrets

```python
client.set_secret("proj-uuid", "DB_PASSWORD", "...")  # encrypted at rest, never returned
client.list_secrets("proj-uuid")                       # names only
client.delete_secret("proj-uuid", "DB_PASSWORD")
```

## Live event stream

```python
for event in client.stream_events(since="2026-10-05T00:00:00Z"):
    print(event["type"], event.get("deployment_id") or event.get("task_id"))
```

## Logs

```python
# Create a `logs` task for a deployment and poll it to terminal.
text = client.get_logs(deployment_id="dep-uuid")

# Or poll an existing logs task by id, and stream chunks as they arrive:
for chunk in client.get_logs(task_id="task-uuid", follow=True):
    print(chunk, end="")
```

## Error shape

Every failure raises `UahtError` with `.code`, `.message`, `.status`, `.body`,
mirroring the protocol error shape `{"error": {"code": ..., "message": ...}}`.

## Tests

```sh
pytest
```
