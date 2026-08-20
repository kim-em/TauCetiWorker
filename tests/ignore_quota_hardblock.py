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


# ...and the ROUND applies the same rule, not just the loop between rounds. A one-shot
# `tauceti work --agent claude --ignore-quota` used to skip the usage read entirely and launch into
# whatever was there, which is the very thing the flag's own help says it does not do.
def resolve(chosen, p, *, agent="claude", pending_init=False, quota_cmd=None):
    """(model, bootstrap) or the NoProgress/SystemExit it raised, with the pacer stubbed."""
    saved_choose, saved_pending = tc.loop.choose_model, tc.loop.claude_pending_init
    calls = []
    tc.loop.choose_model = lambda *a, **k: calls.append((a, k)) or (chosen, {agent: p} if p else {})
    tc.loop.claude_pending_init = lambda _snap: pending_init
    try:
        return tc.loop.resolve_work_model(
            object(), agent, dry=False, ignore_quota=True, quota_cmd=quota_cmd, fresh=True
        ), bool(calls)
    except (tc.NoProgress, SystemExit) as e:
        return str(e), bool(calls)
    finally:
        tc.loop.choose_model, tc.loop.claude_pending_init = saved_choose, saved_pending


def case(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


under = prov(W("session", 9.0, 41.0, "under-pace"), W("weekly", 10.0, 50.0, "under-pace"))
over = prov(W("session", 60.0, 40.0, "over-pace"), W("weekly", 10.0, 50.0, "under-pace"))
dead = prov(W("session", 100.0, 40.0, "exhausted"), W("weekly", 10.0, 50.0, "under-pace"))

case("available → the round runs, having looked", resolve("claude", under), (("claude", False), True))
case("over-pace → the round runs anyway", resolve(None, over), (("claude", False), True))
result, looked = resolve(None, dead)
case("exhausted → the round refuses", ("hard-blocked" in result, looked), (True, True))
case("...naming the condition", "exhausted" in result, True)
result, looked = resolve(None, prov(error="claude usage HTTP 429", retry_after=580))
case("an endpoint that will not answer → the round refuses", ("hard-blocked" in result, looked), (True, True))
case("...naming that too", "429" in result, True)
case(
    "an unopened window is the one hard block a round may still clear",
    resolve(None, dead, pending_init=True),
    (("claude", True), True),
)
# The guards either side of the new read: `auto` cannot be paced by hand, and --quota-cmd replaces the
# pacer outright, so neither reaches the verdict.
refused, looked = resolve(None, under, agent="auto")
case("auto is still refused up front, before any read", (refused[:14], looked), ("--ignore-quota", False))
case("--quota-cmd still decides for itself", resolve("codex", None, quota_cmd="/bin/echo"), (("codex", False), True))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
