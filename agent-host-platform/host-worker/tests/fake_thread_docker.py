"""Thread-safe fake DockerClient for the W13b (§41/§42) test files.

Each "container" is a real HTTP server on its own thread, bound to the
mapped host port — the same port the production health checker polls.
That makes the multi-app and concurrency tests exercise the REAL
deployment pipeline (port pick/reserve, OS bind verification, run,
healthcheck, state persistence, restart, rollback, GC) with deterministic
in-process "containers" instead of a Docker daemon.

Thread-safety: every record mutation is under an RLock, so concurrent
pipeline.deploy() calls from the tests' thread pools behave like the
production claim loop's ThreadPoolExecutor.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _ContainerCfg:
    """Mutable per-container behavior switches (test-controlled)."""

    def __init__(self, app_name: str, healthy: bool = True):
        self.app_name = app_name
        self.healthy = healthy
        self.hits = 0


class _AppHandler(BaseHTTPRequestHandler):
    server_version = "FakeThreadDocker/1"

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        cfg: _ContainerCfg = self.server.cfg  # type: ignore[attr-defined]
        cfg.hits += 1
        if self.path == "/health":
            if cfg.healthy:
                self._send(200, b"ok")
            else:
                self._send(500, b"sick")
        else:
            self._send(200, json.dumps({"app": cfg.app_name}).encode(),
                       "application/json")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wait_listening(port: int, timeout: float = 10.0) -> None:
    """Bounded readiness poll: the server thread must accept connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)),
                                          timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"fake container never listened on :{port}")


class ThreadDockerClient:
    """In-memory stand-in for docker.client.DockerClient (thread-safe)."""

    def __init__(self):
        self._lock = threading.RLock()
        self.containers = {}       # name -> record dict
        self.images = set()
        self.run_calls = []        # dicts: name/image/ports/env
        self.stop_calls = []
        self.start_calls = []
        self.restart_calls = []
        self.rm_calls = []

    # -- helpers -----------------------------------------------------------
    def _new_server(self, port: int, cfg: _ContainerCfg) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), _AppHandler)
        server.cfg = cfg  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever,
                                  name=f"fake-container-{port}",
                                  daemon=True)
        thread.start()
        return server

    def _is_running(self, record) -> bool:
        return record.get("server") is not None

    # -- interface used by the pipeline / handlers / gc --------------------
    def version(self):
        return "fake-thread-1"

    def compose_available(self):
        return False

    def build(self, context_dir, dockerfile, tag, build_args=None,
              timeout=1200):
        with self._lock:
            self.images.add(tag)
        return "fake build output"

    def image_exists(self, tag):
        with self._lock:
            return tag in self.images

    def remove_image(self, tag):
        with self._lock:
            self.images.discard(tag)

    def run(self, name, image, ports=None, env=None, memory=None, cpus=None,
            restart="unless-stopped", timeout=120):
        ports = dict(ports or {})
        env = {str(k): str(v) for k, v in (env or {}).items()}
        host_port = next(iter(ports)) if ports else None
        if host_port is None:
            raise AssertionError("fake docker run requires a published host port")
        cfg = _ContainerCfg(
            app_name=env.get("APP_NAME", name),
            healthy=env.get("HEALTH_FAIL") != "1",
        )
        server = self._new_server(int(host_port), cfg)
        with self._lock:
            if name in self.containers:
                server.shutdown()
                server.server_close()
                raise AssertionError(f"container {name} already exists")
            self.run_calls.append({"name": name, "image": image,
                                   "ports": dict(ports), "env": dict(env)})
            self.containers[name] = {
                "name": name, "image": image, "ports": dict(ports),
                "env": dict(env), "server": server, "cfg": cfg,
                "log_lines": [f"container {name} started on :{host_port}"],
            }
        _wait_listening(int(host_port))
        return f"fake-id-{name}"

    def stop(self, name, timeout_secs=10, timeout=120):
        with self._lock:
            self.stop_calls.append(name)
            record = self.containers.get(name)
            if record is None or not self._is_running(record):
                return
            server = record["server"]
            record["server"] = None
            record["log_lines"].append(f"container {name} stopped")
        server.shutdown()
        server.server_close()

    def start(self, name, timeout=120):
        with self._lock:
            self.start_calls.append(name)
            record = self.containers.get(name)
            if record is None:
                raise AssertionError(f"no such container {name}")
            if self._is_running(record):
                return
            host_port = next(iter(record["ports"]))
            server = self._new_server(int(host_port), record["cfg"])
            record["server"] = server
            record["log_lines"].append(f"container {name} started on :{host_port}")
        _wait_listening(int(host_port))

    def restart_container(self, name, timeout=120):
        with self._lock:
            self.restart_calls.append(name)
        self.stop(name)
        self.start(name)

    def rm(self, name, force=False, timeout=120):
        with self._lock:
            self.rm_calls.append(name)
            record = self.containers.pop(name, None)
            if record is None:
                return
            server = record["server"]
            record["server"] = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def container_exists(self, name):
        with self._lock:
            return name in self.containers

    def container_status(self, name):
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return None
            return "running" if self._is_running(record) else "exited"

    def logs(self, name, tail=500):
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return ""
            lines = list(record["log_lines"])
        return "\n".join(lines[-tail:])

    def ps(self, all=False):
        with self._lock:
            items = list(self.containers.items())
        rows = []
        for name, record in items:
            running = self._is_running(record)
            if not running and not all:
                continue
            ports = record["ports"] if running else {}
            cell = ", ".join(f"0.0.0.0:{hp}->{cp}/tcp"
                             for hp, cp in ports.items())
            rows.append({"Names": "/" + name, "Ports": cell,
                         "State": "running" if running else "exited"})
        return rows

    def inspect(self, name):
        with self._lock:
            record = self.containers.get(name)
            if record is None:
                return []
            running = self._is_running(record)
            ports = record["ports"] if running else {}
        bindings = {f"{cp}/tcp": [{"HostIp": "0.0.0.0",
                                   "HostPort": str(hp)}]
                    for hp, cp in ports.items()}
        return [{"Config": {"Image": record["image"]},
                 "State": {"Status": "running" if running else "exited"},
                 "NetworkSettings": {"Ports": bindings}}]

    # -- test controls ------------------------------------------------------
    def env_of(self, name):
        """The env the container was launched with (leakage assertions)."""
        with self._lock:
            return dict(self.containers[name]["env"])

    def set_healthy(self, name, healthy: bool):
        """Flip /health -> 200/500 on a live container (failure injection)."""
        with self._lock:
            self.containers[name]["cfg"].healthy = healthy

    def http_get(self, port, path="/"):
        """GET http://127.0.0.1:<port><path> -> (status, body)."""
        import urllib.request
        import urllib.error
        url = f"http://127.0.0.1:{int(port)}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def shutdown_all(self):
        with self._lock:
            names = list(self.containers)
        for name in names:
            try:
                self.rm(name, force=True)
            except Exception:
                pass
