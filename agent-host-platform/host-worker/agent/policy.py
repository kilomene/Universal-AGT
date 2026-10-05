"""Task execution POLICY — the command allowlist (PROTOCOL §6).

Policy statement
----------------
The worker MUST NEVER execute an arbitrary shell string received from the
network. Every task type in the protocol maps to exactly one fixed Python
handler (see executor/handlers.py). Each handler builds its own argv list
and invokes subprocess with shell=False (in practice: shell=True never
appears anywhere in this codebase).

Rules enforced here:
  1. Only the 18 protocol task types (§3.2 / §5) are executable. Any other
     ``type`` value — including a missing one — raises TaskRejected and the
     task is reported ``failed`` WITHOUT any execution.
  2. Handlers receive the task dict and a WorkerContext; they may only call
     the docker CLI wrapper (docker/client.py, argv-only), outbound HTTPS
     (agent/api.py), /proc-based metrics, and local files under the
     worker's work_dir / apps_dir.
  3. Filesystem paths from payloads are confined to work_dir / apps_dir
     (handlers reject ".." escapes and absolute escapes).
  4. Secret values (task payload ``secrets``, the host token) are scrubbed
     from every log line before it is written or streamed.

Adding a new task type requires: a handler in executor/handlers.py, an
entry in TASK_HANDLERS below, and a protocol amendment — in that order.
"""
from __future__ import annotations

from executor import handlers


class TaskRejected(Exception):
    """Raised for task types outside the allowlist. Nothing is executed."""


# The complete allowlist: protocol task type -> handler function.
# Keep in sync with PROTOCOL §3.2's type union.
TASK_HANDLERS: dict[str, callable] = {
    # deployment lifecycle
    "deploy": handlers.handle_deploy,
    "restart": handlers.handle_restart,
    "stop": handlers.handle_stop,
    "start": handlers.handle_start,
    "remove": handlers.handle_remove,
    "rollback": handlers.handle_rollback,
    # introspection
    "logs": handlers.handle_logs,
    "status": handlers.handle_status,
    "healthcheck": handlers.handle_healthcheck,
    "system-info": handlers.handle_system_info,
    # builds
    "build": handlers.handle_build,
    "docker-build": handlers.handle_docker_build,
    # raw container ops
    "docker-run": handlers.handle_docker_run,
    "docker-compose": handlers.handle_docker_compose,
    # configuration
    "environment-update": handlers.handle_environment_update,
    # artifacts
    "artifact-download": handlers.handle_artifact_download,
    "artifact-upload": handlers.handle_artifact_upload,
    # ingress
    "ingress-sync": handlers.handle_ingress_sync,
}

# The canonical set of executable types, derived from the allowlist itself.
ALLOWED_TASK_TYPES: frozenset = frozenset(TASK_HANDLERS)


def get_handler(task_type) -> callable:
    """Return the handler for task_type, or raise TaskRejected."""
    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        raise TaskRejected(
            f"task type {task_type!r} is not in the execution allowlist; "
            f"allowed: {sorted(ALLOWED_TASK_TYPES)}"
        )
    return handler


def is_allowed(task_type) -> bool:
    return task_type in TASK_HANDLERS


def reject_disallowed_fields(task_type: str, task: dict) -> None:
    """Raise TaskRejected for payload fields removed for security reasons.

    Nothing is executed when this raises — the dispatcher reports the task
    ``failed`` (rejected) before any handler runs.
    """
    payload = (task or {}).get("payload") or {}
    # 2026-10-05: 'extra_args' on docker-run was an argv-injection path to
    # host root (--privileged, -v /:/host). Removed from the protocol;
    # any task still carrying it is rejected, not executed.
    if task_type == "docker-run" and "extra_args" in payload:
        raise TaskRejected(
            "docker-run no longer accepts 'extra_args' (removed 2026-10-05: "
            "arbitrary docker flags such as --privileged or -v /:/host gave "
            "host root). Remove the field from the payload and re-submit."
        )
