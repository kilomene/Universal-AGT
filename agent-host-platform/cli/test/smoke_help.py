#!/usr/bin/env python3
"""Smoke tests for the agent-host CLI.

1. `agent-host --help` and every subcommand's --help must exit 0, and the
   top-level help must list every subcommand.
2. EVERY command must emit valid JSON on stdout under --json. Commands run
   in-process against a stubbed SDK client (no network); whole-output
   json.loads for one-shot commands, per-line json.loads for the streaming
   follow modes (logs --follow, events --follow emit one JSON object per
   line).
"""

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout

SUBCOMMANDS = ["hosts", "apps", "deploy", "logs", "restart", "stop", "start",
               "status", "rollback", "domains", "tasks", "events",
               "agents", "projects", "deployments", "approve", "reject",
               "cancel", "secrets"]


def check_help():
    failures = []
    invocations = [["agent-host", "--help"]] + [["agent-host", c, "--help"] for c in SUBCOMMANDS]
    for argv in invocations:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            failures.append((" ".join(argv), "agent-host not on PATH"))
            continue
        if r.returncode != 0:
            failures.append((" ".join(argv), r.stderr.strip()[:200]))
    # top-level help must name every subcommand
    r = subprocess.run(["agent-host", "--help"], capture_output=True, text=True, timeout=30)
    for c in SUBCOMMANDS:
        if c not in r.stdout:
            failures.append(("agent-host --help", f"missing subcommand '{c}' in help output"))
    return failures, len(invocations)


class FakeClient:
    """Stubbed UahtClient: canned envelopes, no network."""

    def list_hosts(self, *a, **k):
        return {"hosts": [{"id": "h1", "name": "n1", "status": "online", "worker_version": "1.0"}]}

    def list_services(self, *a, **k):
        return {"services": []}

    def list_projects(self, *a, **k):
        return {"projects": [{"id": "p1", "name": "web"}]}

    def create_deployment(self, *a, **k):
        return {"deployment": {"id": "d1"}, "task": {"id": "t1", "status": "queued"}}

    def create_task(self, *a, **k):
        return {"task": {"id": "t9", "status": "completed",
                         "result": {"logs": "line1\nline2\n"}}}

    def get_task(self, *a, **k):
        return {"task": {"id": "t9", "status": "completed",
                         "result": {"logs": "line1\nline2\n"}}}

    def restart_service(self, *a, **k):
        return {"task": {"id": "t2", "status": "queued"}}

    def stop_service(self, *a, **k):
        return {"task": {"id": "t2", "status": "queued"}}

    def start_service(self, *a, **k):
        return {"task": {"id": "t2", "status": "queued"}}

    def get_deployment(self, *a, **k):
        return {"deployment": {"id": "d1", "status": "running"}}

    def rollback_deployment(self, *a, **k):
        return {"task": {"id": "t3", "status": "queued"}}

    def add_domain(self, *a, **k):
        return {"domain": {"hostname": "ex.com", "status": "dns_pending"}}

    def remove_domain(self, *a, **k):
        return {"removed": True}

    def list_domains(self, *a, **k):
        return {"domains": []}

    def list_tasks(self, *a, **k):
        return {"tasks": []}

    def list_events(self, *a, **k):
        return {"events": []}

    def stream_events(self, *a, **k):
        yield {"id": 1, "type": "task.created", "created_at": "2026-10-05T00:00:00Z"}
        yield {"id": 2, "type": "task.completed", "created_at": "2026-10-05T00:00:01Z"}

    def register_agent(self, *a, **k):
        return {"agent": {"id": "a1", "name": "n"}, "api_key": "sekret"}

    def me(self, *a, **k):
        return {"agent": {"id": "a1", "name": "n"}}

    def create_project(self, *a, **k):
        return {"project": {"id": "p1", "name": "web"}}

    def get_project(self, *a, **k):
        return {"project": {"id": "p1", "name": "web"}}

    def update_project(self, *a, **k):
        return {"project": {"id": "p1", "name": "web"}}

    def list_deployments(self, *a, **k):
        return {"deployments": []}

    def approve_task(self, *a, **k):
        return {"task": {"id": "t1", "status": "queued"}}

    def reject_task(self, *a, **k):
        return {"task": {"id": "t1", "status": "cancelled"}}

    def cancel_task(self, *a, **k):
        return {"task": {"id": "t1", "status": "cancelled"}}

    def set_secret(self, *a, **k):
        return {"secret": {"name": "n"}}

    def delete_secret(self, *a, **k):
        return {"deleted": True}

    def list_secrets(self, *a, **k):
        return {"secrets": []}


# argv (after the global --json) -> True if the command streams JSON lines
JSON_INVOCATIONS = [
    (["hosts"], False),
    (["apps"], False),
    (["deploy", "--project", "web", "--version", "1.0"], False),
    (["logs", "--deployment", "d1"], False),
    (["logs", "--deployment", "d1", "--follow"], True),
    (["restart", "--deployment", "d1"], False),
    (["stop", "--deployment", "d1"], False),
    (["start", "--deployment", "d1"], False),
    (["status", "--deployment", "d1"], False),
    (["rollback", "--deployment", "d1"], False),
    (["domains", "list", "--deployment", "d1"], False),
    (["domains", "add", "--deployment", "d1", "--hostname", "ex.com"], False),
    (["domains", "rm", "--deployment", "d1", "--hostname", "ex.com"], False),
    (["tasks"], False),
    (["events"], False),
    (["events", "--follow"], True),
    (["agents", "me"], False),
    (["agents", "register", "--name", "n1"], False),
    (["projects", "list"], False),
    (["projects", "create", "--name", "web"], False),
    (["projects", "get", "--project", "p1"], False),
    (["projects", "update", "--project", "p1", "--configuration", '{"x": 1}'], False),
    (["deployments"], False),
    (["approve", "--task", "t1"], False),
    (["reject", "--task", "t1"], False),
    (["cancel", "--task", "t1"], False),
    (["secrets", "list", "--project", "p1"], False),
    (["secrets", "set", "--project", "p1", "--name", "n", "--value", "v"], False),
    (["secrets", "delete", "--project", "p1", "--name", "n"], False),
]


def check_json():
    failures = []
    import agent_host_cli.main as main

    orig_client = main._client
    main._client = lambda args: FakeClient()  # noqa: E731 - stubbed, no network
    try:
        for argv, json_lines in JSON_INVOCATIONS:
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    main.main(["--json"] + argv)
            except SystemExit as se:
                if se.code not in (0, None):
                    failures.append((" ".join(argv), f"exit code {se.code}"))
                    continue
            except Exception as exc:  # noqa: BLE001 - report, don't crash the suite
                failures.append((" ".join(argv), f"raised {type(exc).__name__}: {exc}"))
                continue
            out = buf.getvalue()
            try:
                if json_lines:
                    lines = [ln for ln in out.splitlines() if ln.strip()]
                    assert lines, "no output lines"
                    for ln in lines:
                        json.loads(ln)
                else:
                    assert out.strip(), "empty stdout"
                    json.loads(out)
            except (json.JSONDecodeError, AssertionError) as exc:
                failures.append((" ".join(argv), f"invalid JSON: {exc}; output={out[:200]!r}"))
    finally:
        main._client = orig_client
    return failures, len(JSON_INVOCATIONS)


def main():
    failures, n_help = check_help()
    jfailures, n_json = check_json()
    failures.extend(jfailures)
    if failures:
        for argv, why in failures:
            print(f"FAIL {argv}: {why}")
        sys.exit(1)
    print(f"OK: all {n_help} help invocations exited 0, "
          f"help lists all {len(SUBCOMMANDS)} subcommands, "
          f"all {n_json} commands emit valid JSON under --json")


if __name__ == "__main__":
    main()
