# agent-host CLI

Command-line client for the Universal Agent-to-Persistent-Host Deployment
System control plane. Talks to the control plane only — never to host IPs.

## Install

```sh
pip install -e ../agent-sdk/python   # the Python SDK
pip install -e .                     # this CLI (provides the `agent-host` command)
```

## Configuration

```sh
export UAHT_BASE_URL="https://cp.example.com"   # control plane base URL
export UAHT_API_KEY="<your agent API key>"      # never commit this anywhere
```

The `--base-url` / `--api-key` flags override the environment.

## Usage

```sh
agent-host hosts                          # list persistent hosts
agent-host apps                           # list running services
agent-host deploy --project my-api --version 1.2.0 --host host-01
agent-host deploy --project my-api --version 1.2.0 --artifact ./dist/app.tar.gz
agent-host deploy --project my-api --version 1.2.0 --mode manual   # needs approval
agent-host logs --deployment <id> [--follow]
agent-host restart --deployment <id>
agent-host stop --deployment <id>
agent-host start --deployment <id>
agent-host status --deployment <id>
agent-host rollback --deployment <id>
agent-host tasks [--status running]
agent-host events [--follow]              # --follow streams live SSE events
agent-host domains list --deployment <id>
agent-host domains add --deployment <id> --hostname api.example.com
agent-host domains rm --deployment <id> --hostname api.example.com
```

Append `--json` to any command for machine-readable output; otherwise a
human-readable table is printed.

`agent-host logs --deployment <id>` creates a `logs` task and waits for it
(protocol §5: task `result.logs`). With `--follow` it streams the log output
as the task appends it.

`agent-host deploy --artifact FILE` uploads the file first (sha256 is computed
client-side per protocol §3.5) and then deploys the resulting artifact id.
