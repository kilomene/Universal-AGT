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
# export UAHT_PROVISIONING_TOKEN="<token>"      # only for agents/hosts registration
```

The `--base-url` / `--api-key` / `--provisioning-token` flags override the
environment. `agents register` and `hosts register` use the provisioning
token (`X-Provisioning-Token` header) — no agent key needed; `hosts register`
also accepts an agent key with the `deploy` permission.

## Usage

```sh
agent-host hosts [list|register|get]        # register needs --name (+ --provisioning-token or a deploy key)
agent-host apps [--host <name|id>]      # list running services (name resolves to the host id)
agent-host deploy --project my-api --version 1.2.0 --host host-01
agent-host deploy --project my-api --version 1.2.0 --artifact ./dist/app.tar.gz
agent-host deploy --project my-api --version 1.2.0 --mode manual   # needs approval
agent-host logs --deployment <id> [--follow]
agent-host restart --deployment <id>
agent-host stop --deployment <id>
agent-host start --deployment <id>
agent-host status --deployment <id>
agent-host rollback --deployment <id>
agent-host tasks [list|status] [--status running]   # status needs --task <id>
agent-host events [--follow]              # --follow streams live SSE events
agent-host agents register|me|rotate      # register needs --provisioning-token (or UAHT_PROVISIONING_TOKEN)
agent-host projects create|list|get|update
agent-host deployments list               # --project, --host, --status filters
agent-host approve|reject --task <id>     # manual-mode gate (needs approve_deployments)
agent-host cancel --task <id>             # needs deploy or creatorship
agent-host secrets set|list|delete --project <name>   # names only are ever listed
agent-host domains list --deployment <id>
agent-host domains get --hostname api.example.com
agent-host domains add --deployment <id> --hostname api.example.com [--ingress tunnel|direct]
agent-host domains rm --deployment <id> --hostname api.example.com
```

19 subcommands (see `agent-host --help`); every one emits valid JSON
under `--json` (verified by `test/smoke_help.py`).

Append `--json` to any command for machine-readable output; otherwise a
human-readable table is printed.

`agent-host logs --deployment <id>` creates a `logs` task and waits for it
(protocol §5: task `result.logs`). With `--follow` it streams the log output
as the task appends it.

`agent-host deploy --artifact FILE` uploads the file first (sha256 is computed
client-side per protocol §3.5) and then deploys the resulting artifact id.
