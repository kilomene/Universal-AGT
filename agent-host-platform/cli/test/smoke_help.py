#!/usr/bin/env python3
"""Smoke test: `agent-host --help` and every subcommand's --help must exit 0."""

import subprocess
import sys

SUBCOMMANDS = ["hosts", "apps", "deploy", "logs", "restart", "stop", "start",
               "status", "rollback", "domains", "tasks", "events",
               "agents", "projects", "deployments", "approve", "reject",
               "cancel", "secrets"]

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

if failures:
    for argv, why in failures:
        print(f"FAIL {argv}: {why}")
    sys.exit(1)
print(f"OK: all {len(invocations)} help invocations exited 0")
