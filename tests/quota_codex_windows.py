#!/usr/bin/env python3
"""_codex_from_payload must survive the codex usage endpoint reporting a single window.

After a codex-cli upgrade the ChatGPT usage endpoint stopped returning the short `secondary_window` for
pro accounts: it now reports one weekly window in `primary_window` and `secondary_window: null`. The old
positional parse (primary→'session', secondary→'weekly') read that null as a window with no `used_percent`,
classified it 'unknown', and pinned codex out with 'usage unknown' even though the account had full quota.

An explicitly null/absent window must be SKIPPED (it does not apply to the plan), NOT read as unknown. But
the fail-closed guard for a window that IS present yet drops `used_percent` must stay: that is real schema
drift and must never silently unlock spending. And a payload with NO usable window must fail-CLOSED, never
fail-open on a vacuously-true all(...). Dependency-free; no network.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


# A bare Quota is enough: _codex_from_payload only reaches self via the _next_eligible staticmethod.
q = tc.Quota.__new__(tc.Quota)


def win(used, lim, ra):
    return {"used_percent": used, "limit_window_seconds": lim, "reset_after_seconds": ra}


WEEK = 604800
HOUR5 = 5 * 3600

# The new single-window schema: a fresh weekly window, secondary explicitly null. Codex is AVAILABLE, and
# the one window is labelled by its length ('weekly'), not by its 'primary' slot.
p = q._codex_from_payload(
    {"rate_limit": {"limit_reached": False, "primary_window": win(0, WEEK, WEEK), "secondary_window": None}}
)
check("single weekly window ⇒ available", p.available, True)
check("single weekly window ⇒ gpt-5", p.model, "gpt-5")
check("null secondary is skipped, only one window emitted", len(p.windows), 1)
check("window named by length, not slot", p.windows[0].name, "weekly")
check("no spurious 'unknown' window", [w.status for w in p.windows], ["under-pace"])

# The old two-window schema still parses: short window→'session', long→'weekly', both under pace ⇒ available.
p = q._codex_from_payload(
    {
        "rate_limit": {
            "limit_reached": False,
            "primary_window": win(9, HOUR5, HOUR5 // 2),
            "secondary_window": win(10, WEEK, WEEK // 2),
        }
    }
)
check("two-window schema still available", p.available, True)
check("two-window schema names by length", [w.name for w in p.windows], ["session", "weekly"])

# Fail-closed guard preserved: a window that IS present but drops used_percent reads as unknown ⇒ NOT available.
p = q._codex_from_payload(
    {
        "rate_limit": {
            "limit_reached": False,
            "primary_window": {"limit_window_seconds": WEEK, "reset_after_seconds": WEEK // 2},
            "secondary_window": None,
        }
    }
)
check("present window missing used_percent ⇒ unknown", p.windows[0].status, "unknown")
check("present window missing used_percent ⇒ unavailable", p.available, False)

# A present-but-malformed window (not null, not an object) is corruption we cannot read, NOT a
# 'not-applicable' signal like null ⇒ it must fail-CLOSED (unknown), never be silently dropped.
for bad in ("corrupt", [], 42):
    p = q._codex_from_payload(
        {"rate_limit": {"limit_reached": False, "primary_window": win(0, WEEK, WEEK), "secondary_window": bad}}
    )
    check(f"malformed secondary {bad!r} ⇒ unknown window", [w.status for w in p.windows], ["under-pace", "unknown"])
    check(f"malformed secondary {bad!r} ⇒ unavailable (fail-closed)", p.available, False)

# No usable window at all ⇒ fail-CLOSED, never fail-open on a vacuous all(...).
p = q._codex_from_payload({"rate_limit": {"limit_reached": False, "primary_window": None, "secondary_window": None}})
check("no windows ⇒ unavailable (fail-closed)", p.available, False)
check("no windows ⇒ empty window list", p.windows, [])
soft, why = tc._unavail_reason(p)
check("no windows is a HARD block", soft, False)

# limit_reached still dominates even with a healthy-looking window.
p = q._codex_from_payload(
    {"rate_limit": {"limit_reached": True, "primary_window": win(0, WEEK, WEEK), "secondary_window": None}}
)
check("limit_reached ⇒ exhausted", p.windows[0].status, "exhausted")
check("limit_reached ⇒ unavailable", p.available, False)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
