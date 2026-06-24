#!/usr/bin/env python3
"""--ignore-quota overrides PACING, not AVAILABILITY.

A worker pinned to `--agent claude --ignore-quota` is meant to skip the burn-pace throttle, not to fire
review rounds into a provider that is actually out. So the loop still reads the usage endpoint and treats
a HARD block — a window at 100% (exhausted), usage it cannot read (fail-closed), or the endpoint itself
refusing to answer (its own 429 / a network error, which leaves the Provider with no windows) — as a
reason to wait. Only a SOFT over-pace block (real quota left, merely ahead of pace) is run through.

Regression for the loop that re-reviewed the same green PRs up to the daily cap during a subscription
rate-limit, burning a clone + engine launch each round to post an all-error scoreboard. Dependency-free.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc


def W(name, used, elapsed, status):
    return tc.Window(name, used, elapsed, None, status)


def prov(*windows, error=None, retry_after=None, next_eligible=None):
    return tc.Provider("claude", False, None, list(windows), error, next_eligible, retry_after)


# (name, chosen, prov, expected verdict)
cases = [
    (
        "available → run",
        "claude",
        prov(W("session", 9.0, 41.0, "under-pace"), W("weekly", 10.0, 50.0, "under-pace")),
        "run",
    ),
    (
        "over-pace is soft → --ignore-quota runs anyway",
        None,
        prov(W("session", 36.0, 23.0, "over-pace"), W("weekly", 32.0, 86.0, "under-pace")),
        "over-pace",
    ),
    (
        "exhausted window (100%) is a hard block → wait",
        None,
        prov(W("session", 100.0, 50.0, "exhausted"), W("weekly", 20.0, 80.0, "under-pace")),
        "wait",
    ),
    (
        "unreadable usage is a hard block → wait (fail-closed)",
        None,
        prov(W("session", None, None, "unknown"), W("weekly", 20.0, 80.0, "under-pace")),
        "wait",
    ),
    (
        "mixed unknown + over-pace is hard, not soft → wait (fail-closed)",
        None,
        prov(W("session", None, None, "unknown"), W("weekly", 60.0, 40.0, "over-pace")),
        "wait",
    ),
    (
        "usage endpoint refused to answer (429, no windows) → wait",
        None,
        prov(error="claude usage HTTP 429", retry_after=580),
        "wait",
    ),
    (
        "no snapshot for the agent at all → wait (fail-closed)",
        None,
        None,
        "wait",
    ),
]

fails = 0
for name, chosen, p, expected in cases:
    got = tc._ignore_quota_verdict(chosen, p)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={expected!r}")
    fails += not ok

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
