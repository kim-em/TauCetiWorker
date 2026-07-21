#!/usr/bin/env python3
"""_claude_from_payload reads the Claude usage endpoint, whose schema moved from flat `five_hour` /
`seven_day` objects to a structured `limits` array (kind=session|weekly_all|weekly_scoped). The reader
must: prefer the array, fall back to the flat keys, gate opus on the session + the unscoped weekly only
(never a per-model weekly cap), and no longer emit a `weekly_sonnet` window (the old key is now null).
Dependency-free; no network. Uses a far-future reset so 0%% usage is deterministically under pace and
100%% is exhausted regardless of the wall clock."""

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


# _claude_from_payload only reaches self via _parse_iso / _next_eligible (staticmethods) and the
# _claude_win* helpers, none of which need cfg — a bare instance is enough.
q = tc.Quota.__new__(tc.Quota)
FUTURE = "2099-01-01T00:00:00Z"  # remaining >> window ⇒ elapsed clamps to 0 ⇒ used 0 is under pace


def limits(session_pct, weekly_pct, *, scoped_pct=99):
    return {
        "limits": [
            {"kind": "session", "group": "session", "percent": session_pct, "resets_at": FUTURE, "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_pct, "resets_at": FUTURE, "scope": None},
            # a per-model weekly cap (here maxed out) — must be ignored, never gate opus:
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": scoped_pct,
                "resets_at": None,
                "scope": {"model": {"display_name": "Fable"}},
            },
        ],
        # flat keys absent/null in the new schema — the array must be used, and null sonnet ignored:
        "five_hour": None,
        "seven_day": None,
        "seven_day_sonnet": None,
        "seven_day_opus": None,
    }


# --- new `limits` schema ---------------------------------------------------------------------------
p = q._claude_from_payload(limits(0, 0))
check("limits: exactly two gating windows (no sonnet/scoped)", [w.name for w in p.windows], ["session", "weekly"])
check("limits: session/weekly percents parsed", [w.used for w in p.windows], [0, 0])
check("limits: both under pace ⇒ available opus", (p.available, p.model), (True, "opus"))
check("limits: a maxed per-model (scoped) weekly does NOT block opus", p.available, True)

p = q._claude_from_payload(limits(0, 100))
check("limits: weekly at 100%% ⇒ exhausted ⇒ unavailable", (p.available, p.model), (False, None))

# --- flat fallback (no usable `limits`) ------------------------------------------------------------
flat = {
    "five_hour": {"utilization": 0, "resets_at": FUTURE},
    "seven_day": {"utilization": 0, "resets_at": FUTURE},
    "seven_day_sonnet": None,  # present-but-null: must be ignored, not a third window
}
p = q._claude_from_payload(flat)
check("flat: two windows, no sonnet", [w.name for w in p.windows], ["session", "weekly"])
check("flat: available opus", (p.available, p.model), (True, "opus"))

# an empty limits array falls back to the flat keys
p = q._claude_from_payload({"limits": [], **flat})
check("empty limits ⇒ flat fallback used", (len(p.windows), p.available), (2, True))

# limits with only a session (no unscoped weekly) falls back to flat
partial = {"limits": [{"group": "session", "percent": 0, "resets_at": FUTURE}], **flat}
p = q._claude_from_payload(partial)
check("limits missing the weekly ⇒ flat fallback", (len(p.windows), p.available), (2, True))

# --- all-null fails closed -------------------------------------------------------------------------
p = q._claude_from_payload({"five_hour": None, "seven_day": None})
check("all usage null ⇒ unavailable", (p.available, p.error), (False, "all usage null"))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
