"""Policy allowlist tests.

  * every one of the 18 protocol task types maps to a handler
  * unknown / missing types are rejected with TaskRejected (no execution)
  * static check: no worker source uses shell=True, and no subprocess call
    takes a plain string as its command (argv lists only) in code that can
    touch network-derived data
"""
import ast
import inspect
from pathlib import Path

import pytest

from agent import policy

# The 18 task types from PROTOCOL §3.2 / §5.
PROTOCOL_TASK_TYPES = [
    "deploy", "restart", "stop", "start", "remove", "rollback",
    "logs", "status", "healthcheck", "build", "docker-build",
    "docker-run", "docker-compose", "environment-update",
    "artifact-download", "artifact-upload", "system-info",
    "ingress-sync",
]


def test_all_18_types_mapped():
    assert len(PROTOCOL_TASK_TYPES) == 18
    for task_type in PROTOCOL_TASK_TYPES:
        assert policy.is_allowed(task_type), f"{task_type} not in allowlist"
        handler = policy.get_handler(task_type)
        assert callable(handler), f"{task_type} handler not callable"


def test_allowlist_matches_protocol_exactly():
    assert set(policy.ALLOWED_TASK_TYPES) == set(PROTOCOL_TASK_TYPES)


def test_every_handler_has_ctx_task_signature():
    for task_type in PROTOCOL_TASK_TYPES:
        sig = inspect.signature(policy.get_handler(task_type))
        assert list(sig.parameters) == ["ctx", "task"], task_type


@pytest.mark.parametrize("bad", [
    "rm -rf /", "reboot", "exec", "shell", "eval", "", None, 42,
    "DEPLOY", "Deploy", "docker_run", "system_info",
])
def test_unknown_types_rejected(bad):
    with pytest.raises(policy.TaskRejected):
        policy.get_handler(bad)


def test_rejected_task_reports_failed_without_executing():
    """An unknown task type must fail closed; the handler is never called."""
    from executor.dispatcher import TaskDispatcher

    calls = []

    class FakeAPI:
        def progress(self, task_id, payload):
            calls.append(payload)
            return {"task": {}}

    class FakeConfig:
        host_token = "tok"

    class FakeLogStore:
        def task_log_path(self, task_id):
            return Path(f"/tmp/uaht-test-{task_id}.log")

        def append_task(self, task_id, text):
            pass

    class FakeCtx:
        config = FakeConfig()
        scrub = staticmethod(lambda s: s)

        def __init__(self):
            self.log_store = FakeLogStore()

        def log(self, task_id, line):
            return line

    dispatcher = TaskDispatcher(FakeCtx(), FakeAPI())
    outcome = dispatcher.dispatch(
        {"id": "t-reject-1", "type": "rm -rf /", "payload": {}})
    assert outcome["status"] == "failed"
    assert outcome["rejected"] is True
    # the failure was reported to the control plane
    assert calls and calls[-1]["status"] == "failed"
    assert "allowlist" in calls[-1]["error"]


# ---------------------------------------------------------------------------
# static source audit: no shell=True anywhere; no string-form subprocess calls
# ---------------------------------------------------------------------------
WORKER_ROOT = Path(__file__).resolve().parent.parent
AUDITED_PACKAGES = ["agent", "executor", "deployments", "docker",
                    "health", "logs", "updater", "ingress"]
SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}


def _iter_py_files():
    for pkg in AUDITED_PACKAGES:
        for path in (WORKER_ROOT / pkg).rglob("*.py"):
            yield path


def _is_subprocess_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_FUNCS:
        val = func.value
        return (isinstance(val, ast.Name) and val.id == "subprocess") or \
               (isinstance(val, ast.Attribute) and val.attr == "subprocess")
    if isinstance(func, ast.Name) and func.id in SUBPROCESS_FUNCS:
        return True
    return False


def test_no_shell_true_anywhere():
    offenders = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_call(node):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"shell=True found in: {offenders}"


def test_no_string_command_subprocess_calls():
    """subprocess must receive argv lists, never a plain command string."""
    offenders = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_call(node):
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"string-form subprocess call in: {offenders}"


def test_os_system_and_popen_shell_absent():
    offenders = []
    for path in _iter_py_files():
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.split("#")[0]
            if "os.system(" in stripped or "os.popen(" in stripped:
                offenders.append(f"{path}:{lineno}")
    assert not offenders, f"os.system/popen found in: {offenders}"
