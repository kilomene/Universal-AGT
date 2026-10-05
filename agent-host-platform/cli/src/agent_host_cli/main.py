"""agent-host CLI — talks to the control plane only, never to host IPs.

Configuration via env vars (UAHT_BASE_URL, UAHT_API_KEY) or the
--base-url / --api-key flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from uaht_sdk import UahtClient, UahtError


def _client(args) -> UahtClient:
    base_url = args.base_url or os.environ.get("UAHT_BASE_URL")
    api_key = args.api_key or os.environ.get("UAHT_API_KEY")
    if not base_url:
        raise SystemExit("error: control plane base URL required — set UAHT_BASE_URL or pass --base-url")
    if not api_key:
        raise SystemExit("error: agent API key required — set UAHT_API_KEY or pass --api-key")
    return UahtClient(base_url=base_url, api_key=api_key)


def _emit(args, data):
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print_table(data)


def print_table(data):
    """Render a list of dicts (or a single dict) as a plain-text table."""
    if isinstance(data, dict):
        rows = [data]
    else:
        rows = list(data)
    if not rows:
        print("(none)")
        return
    keys = list(rows[0].keys())
    widths = {k: max(len(str(k)), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    print("  ".join(str(k).ljust(widths[k]) for k in keys))
    print("  ".join("-" * widths[k] for k in keys))
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))


def _row(resp, key):
    obj = resp.get(key, resp)
    if isinstance(obj, list):
        return obj
    return [obj]


# -- commands ----------------------------------------------------------

def cmd_hosts(args):
    c = _client(args)
    hosts = c.list_hosts()
    rows = hosts.get("hosts", []) if isinstance(hosts, dict) else hosts
    _emit(args, [{"id": h.get("id"), "name": h.get("name"), "status": h.get("status"),
                  "worker_version": h.get("worker_version")} for h in rows])


def cmd_apps(args):
    c = _client(args)
    services = c.list_services(host_id=args.host)
    rows = services.get("services", []) if isinstance(services, dict) else services
    _emit(args, [{"id": s.get("id"), "deployment": s.get("deployment_id"), "project": s.get("project_id"),
                  "host": s.get("host_id"), "status": s.get("status"),
                  "health": s.get("health_status")} for s in rows])


def cmd_deploy(args):
    c = _client(args)
    artifact_id = None
    project_id = _resolve_project_id(c, args.project)

    if args.artifact:
        if not os.path.isfile(args.artifact):
            raise SystemExit(f"error: artifact file not found: {args.artifact}")
        print(f"uploading artifact {args.artifact} ...", file=sys.stderr)
        info = c.init_artifact(project_id, args.artifact, version=args.version)
        c.upload_artifact(info["upload_url"], args.artifact)
        artifact_id = info["artifact"]["id"]
        print(f"artifact uploaded: {artifact_id}", file=sys.stderr)

    host_id = None
    if args.host:
        host_id = _resolve_host_id(c, args.host)

    created = c.create_deployment(
        project_id=project_id,
        version=args.version,
        host_id=host_id,
        artifact_id=artifact_id,
        mode=args.mode or "automatic",
    )
    _emit(args, created)


def _resolve_project_id(c, name_or_id):
    rows = (c.list_projects() or {}).get("projects", []) or []
    m = next((p for p in rows if p.get("name") == name_or_id or p.get("id") == name_or_id), None)
    if not m:
        raise SystemExit(f"error: project '{name_or_id}' not found")
    return m["id"]


def _resolve_host_id(c, name_or_id):
    rows = (c.list_hosts() or {}).get("hosts", []) or []
    m = next((h for h in rows if h.get("name") == name_or_id or h.get("id") == name_or_id), None)
    if not m:
        raise SystemExit(f"error: host '{name_or_id}' not found")
    return m["id"]


def _wait_task(c, task_id, poll_interval=3.0, timeout=600.0):
    terminal = {"completed", "failed", "cancelled"}
    started = time.monotonic()
    while True:
        cur = c.get_task(task_id)
        task = cur.get("task", cur)
        if task.get("status") in terminal:
            return task
        if time.monotonic() - started > timeout:
            raise UahtError("timeout", f"task {task_id} did not finish within {timeout}s")
        time.sleep(poll_interval)


def cmd_logs(args):
    c = _client(args)
    created = c.create_task(type="logs", payload={"deployment_id": args.deployment})
    task = created.get("task", created)
    if args.follow:
        print("following task logs (Ctrl-C to stop) ...", file=sys.stderr)
        last_len = 0
        while True:
            cur = c.get_task(task["id"]).get("task", {})
            logs = (cur.get("result") or {}).get("logs", "") if isinstance(cur.get("result"), dict) else ""
            chunk = logs[last_len:]
            if chunk:
                print(chunk, end="" if chunk.endswith("\n") else "\n")
                last_len = len(logs)
            if cur.get("status") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2.0)
    else:
        task = _wait_task(c, task["id"])
        result = task.get("result") or {}
        logs = result.get("logs", "") if isinstance(result, dict) else str(result)
        if args.json:
            print(json.dumps({"task_id": task["id"], "status": task.get("status"), "logs": logs}, indent=2))
        else:
            print(logs)


def cmd_restart(args):
    c = _client(args)
    _emit(args, c.restart_service(args.deployment))


def cmd_stop(args):
    c = _client(args)
    _emit(args, c.stop_service(args.deployment))


def cmd_start(args):
    c = _client(args)
    _emit(args, c.start_service(args.deployment))


def cmd_status(args):
    c = _client(args)
    _emit(args, c.get_deployment(args.deployment))


def cmd_rollback(args):
    c = _client(args)
    _emit(args, c.rollback_deployment(args.deployment))


def cmd_domains(args):
    c = _client(args)
    if args.action == "add":
        _emit(args, c.add_domain(args.deployment, args.hostname))
    elif args.action == "rm":
        _emit(args, c.remove_domain(args.deployment, args.hostname))
    else:
        domains = c.list_domains(args.deployment)
        rows = domains.get("domains", []) if isinstance(domains, dict) else domains
        _emit(args, [{"hostname": d.get("hostname"), "status": d.get("status"),
                      "added": d.get("added_at")} for d in rows])


def cmd_tasks(args):
    c = _client(args)
    tasks = c.list_tasks(status=args.status)
    rows = tasks.get("tasks", []) if isinstance(tasks, dict) else tasks
    _emit(args, [{"id": t.get("id"), "type": t.get("type"), "status": t.get("status"),
                  "host": t.get("assigned_to"), "created": t.get("created_at")} for t in rows])


def cmd_events(args):
    c = _client(args)
    if args.follow:
        try:
            for ev in c.stream_events():
                if args.json:
                    print(json.dumps(ev, default=str))
                else:
                    print(f"{ev.get('id')}  {ev.get('type')}  {ev.get('created_at', '')}")
        except KeyboardInterrupt:
            pass
    else:
        events = c.list_events()
        rows = events.get("events", []) if isinstance(events, dict) else events
        _emit(args, [{"id": e.get("id"), "type": e.get("type"), "actor": e.get("actor_id"),
                      "created": e.get("created_at")} for e in rows])


# -- argparse ----------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="agent-host",
        description="CLI for the Universal Agent-to-Persistent-Host Deployment System control plane.",
    )
    p.add_argument("--base-url", default=None, help="control plane base URL (or UAHT_BASE_URL)")
    p.add_argument("--api-key", default=None, help="agent API key (or UAHT_API_KEY)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("hosts", help="list persistent hosts")

    a = sub.add_parser("apps", help="list running services (deployments)")
    a.add_argument("--host", default=None, help="filter by host name or id")

    d = sub.add_parser("deploy", help="deploy a project version")
    d.add_argument("--project", required=True, help="project name or id")
    d.add_argument("--version", required=True, help="version label")
    d.add_argument("--host", default=None, help="target host name or id")
    d.add_argument("--mode", choices=["automatic", "manual"], default="automatic",
                   help="automatic (default) or manual (awaiting approval)")
    d.add_argument("--artifact", default=None, help="local file to upload as the artifact first")

    l = sub.add_parser("logs", help="fetch logs for a deployment")
    l.add_argument("--deployment", required=True, help="deployment id")
    l.add_argument("--follow", action="store_true", help="stream log output until the task ends")

    for name, help_text in [
        ("restart", "restart a deployment/service"),
        ("stop", "stop a deployment/service"),
        ("start", "start a deployment/service"),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--deployment", required=True, help="deployment/service id")

    st = sub.add_parser("status", help="show deployment status")
    st.add_argument("--deployment", required=True, help="deployment id")

    r = sub.add_parser("rollback", help="roll a deployment back to the previous healthy version")
    r.add_argument("--deployment", required=True, help="deployment id")

    d = sub.add_parser("domains", help="manage public hostnames for a deployment (optional Cloudflare DNS)")
    d.add_argument("action", nargs="?", default="list", choices=["list", "add", "rm"],
                   help="list domains (default), add one, or remove one")
    d.add_argument("--deployment", required=True, help="deployment id")
    d.add_argument("--hostname", default=None, help="hostname for add/rm")

    t = sub.add_parser("tasks", help="list tasks in the durable queue")
    t.add_argument("--status", default=None, help="filter by task status")

    e = sub.add_parser("events", help="show the append-only event journal")
    e.add_argument("--follow", action="store_true", help="follow the live SSE stream")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        {
            "hosts": cmd_hosts,
            "apps": cmd_apps,
            "deploy": cmd_deploy,
            "logs": cmd_logs,
            "restart": cmd_restart,
            "stop": cmd_stop,
            "start": cmd_start,
            "status": cmd_status,
            "rollback": cmd_rollback,
            "domains": cmd_domains,
            "tasks": cmd_tasks,
            "events": cmd_events,
        }[args.command](args)
    except UahtError as err:
        print(f"error [{err.code}]: {err.message}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
