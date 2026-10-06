"""Service health checking: outbound HTTP GET against the mapped host port.

Used by the deploy pipeline to gate "healthy" before marking a deployment
running. This only opens outbound client connections to 127.0.0.1 — the
worker never listens.
"""
from __future__ import annotations

import time

import requests

DEFAULT_TIMEOUT_SECS = 120
DEFAULT_INTERVAL_SECS = 2


def check_once(url: str, timeout: int = 5) -> bool:
    """Single GET; True only on HTTP 200.

    Redirects are NEVER followed (§19): a 3xx is not a healthy app, and
    following a Location header would turn the health checker into an
    SSRF oracle — a malicious app could 302 to an arbitrary URL (e.g.
    cloud metadata) and learn from the boolean result whether it
    returned 200.
    """
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=False)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def wait_for_healthcheck(host_port: int, path: str = "/",
                         timeout_secs: int = DEFAULT_TIMEOUT_SECS,
                         interval_secs: int = DEFAULT_INTERVAL_SECS,
                         log=None) -> bool:
    """Poll GET http://127.0.0.1:<host_port><path> until 200 or timeout."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://127.0.0.1:{int(host_port)}{path}"
    deadline = time.monotonic() + max(1, timeout_secs)
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if check_once(url):
            if log:
                log(f"healthcheck passed: {url} (attempt {attempt})")
            return True
        if log and attempt % 10 == 1:
            log(f"healthcheck waiting: {url} (attempt {attempt})")
        time.sleep(interval_secs)
    if log:
        log(f"healthcheck FAILED: {url} did not return 200 within {timeout_secs}s")
    return False
