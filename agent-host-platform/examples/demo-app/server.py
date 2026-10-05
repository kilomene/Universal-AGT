#!/usr/bin/env python3
"""Universal-AGT demo app — zero dependencies, Python standard library only.

Routes:
  GET /        -> hello page (HTML, shows app name/version/hostname)
  GET /health  -> 200 {"ok": true}            (the worker's healthcheck path)
  GET /version -> 200 {"name": ..., "version": ...}

Environment:
  PORT        - listen port (default 3000; the worker maps the manifest's
                service.port here via docker -p host:container)
  APP_NAME    - shown on the hello page (default "demo-app")
  APP_VERSION - shown on the hello page and /version (default "0.0.0")
  HEALTH_FAIL - when set to "1", /health returns 500 (used by the E2E suite's
                health-fail-rollback scenario; never set in production use)
"""
from __future__ import annotations

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "3000"))
APP_NAME = os.environ.get("APP_NAME", "demo-app")
APP_VERSION = os.environ.get("APP_VERSION", "0.0.0")
HEALTH_FAIL = os.environ.get("HEALTH_FAIL", "") == "1"


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoApp/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0]
        if path == "/health":
            if HEALTH_FAIL:
                self._json(500, {"ok": False, "error": "simulated unhealthy"})
            else:
                self._json(200, {"ok": True})
            return
        if path == "/version":
            self._json(200, {"name": APP_NAME, "version": APP_VERSION})
            return
        if path == "/":
            page = (
                "<!doctype html><html><head><title>{name}</title></head>"
                "<body><h1>Hello from {name} 🎉</h1>"
                "<p>version {version} · host {host}</p>"
                "<p>deployed by Universal-AGT</p></body></html>"
            ).format(name=APP_NAME, version=APP_VERSION,
                     host=socket.gethostname())
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:  # quieter logs
        pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"{APP_NAME} {APP_VERSION} listening on port {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
