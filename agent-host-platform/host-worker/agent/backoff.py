"""Exponential backoff with jitter for control-plane reconnects.

Pure functions — no I/O, no threads — so the retry math is unit-testable.
The loops in agent.main drive these from their consecutive-failure counters
and reset the counter to 0 on any successful response.

Sequence for base=5, cap=300 (seconds):
    failures=1 -> ~5, failures=2 -> ~10, failures=3 -> ~20, ...
    doubling until the cap; each value then gets +/-25% uniform jitter.

Delays are consumed with ``stop_event.wait(delay)`` (never ``sleep``) so
SIGTERM/SIGINT stays responsive during backoff.
"""
from __future__ import annotations

import random
from typing import Callable

DEFAULT_BASE_SECS = 5.0
DEFAULT_CAP_SECS = 300.0
DEFAULT_JITTER_RATIO = 0.25


def backoff_delay(failures: int,
                  base: float = DEFAULT_BASE_SECS,
                  cap: float = DEFAULT_CAP_SECS,
                  jitter_ratio: float = DEFAULT_JITTER_RATIO,
                  rand: Callable[[], float] | None = None) -> float:
    """Delay in seconds before the next attempt after `failures` in a row.

    `failures` must be >= 1. The jitter ratio is the max relative spread:
    the returned value lies within ``delay * (1 +/- jitter_ratio)``.
    `rand` is an injectable ``random.random`` replacement for tests.
    """
    if failures < 1:
        raise ValueError(f"failures must be >= 1, got {failures}")
    if base <= 0:
        raise ValueError(f"base must be > 0, got {base}")
    if cap <= 0:
        raise ValueError(f"cap must be > 0, got {cap}")
    delay = min(cap, base * (2.0 ** (failures - 1)))
    draw = (rand or random.random)()
    jitter = 1.0 + (draw * 2.0 - 1.0) * jitter_ratio
    return max(0.0, delay * jitter)
