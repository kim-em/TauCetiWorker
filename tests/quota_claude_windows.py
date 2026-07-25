#!/usr/bin/env python3
"""_claude_from_payload reads the Claude usage endpoint, whose schema moved from flat `five_hour` /
`seven_day` objects to a structured `limits` array (kind=session|weekly_all|weekly_scoped). The reader
must: prefer the array, fall back to the flat keys, gate opus on the session + the unscoped weekly only
(never a per-model weekly cap), and no longer emit a `weekly_sonnet` window (the old key is now null).
Dependency-free; no network. Uses a far-future reset so 0%% usage is deterministically under pace and
100%% is exhausted regardless of the wall clock."""

import os
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

# --- just-reset windows must not deadlock the worker -----------------------------------------------
# The 5h/weekly windows briefly report with no usage AND no reset clock the moment they roll, before the
# next request opens the new cycle. That is not corrupt telemetry: the worker won't launch claude while a
# window reads 'unknown', but only launching claude opens the next window — so failing closed here would
# be a fixed-point deadlock, recurring on every reset. Both representations Anthropic has been seen to use
# (an absent/null window, and a present window with a null reset clock) must let the worker keep pacing.
os.environ.pop("TAUCETI_PACE", None)  # legacy identity curve ⇒ budget(0)==0, so usage>0 is over pace at elapsed 0

# a just-reset SESSION reported as null, with the weekly still live ⇒ gate on the weekly, stay available
p = q._claude_from_payload({"five_hour": None, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
check("idle session (null) skipped ⇒ gate weekly ⇒ available", (p.available, p.model), (True, "opus"))
check("idle session (null) skipped ⇒ only the weekly gates", [w.name for w in p.windows], ["weekly"])

# a just-reset WEEKLY reported as null, with the session still live ⇒ gate on the session, stay available.
# This is the symmetric case: the fix must NOT depend on the weekly being readable, or it would re-break
# every 7 days when the weekly itself resets.
p = q._claude_from_payload({"five_hour": {"utilization": 0, "resets_at": FUTURE}, "seven_day": None})
check("idle weekly (null) skipped ⇒ gate session ⇒ available", (p.available, p.model), (True, "opus"))
check("idle weekly (null) skipped ⇒ only the session gates", [w.name for w in p.windows], ["session"])

# a just-reset window reported present but with a null reset clock ⇒ read as fresh (0% used, under pace),
# not 'unknown'. Covered for both the flat schema and the new limits array.
p = q._claude_from_payload({"five_hour": {"utilization": 0, "resets_at": None}, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
sess = next(w for w in p.windows if w.name == "session")
check("clockless 0%% session ⇒ fresh/under pace (not unknown)", (sess.status, sess.resets_at), ("under-pace", None))
check("clockless 0%% session ⇒ available", (p.available, len(p.windows)), (True, 2))

p = q._claude_from_payload(
    {
        "limits": [
            {"group": "session", "percent": 0, "resets_at": None, "scope": None},
            {"group": "weekly", "percent": 0, "resets_at": FUTURE, "scope": None},
        ]
    }
)
check("clockless 0%% session in limits ⇒ available", (p.available, p.model), (True, "opus"))

# both windows just-reset/absent at once (a rare simultaneous roll) still fails closed — matches policy.
p = q._claude_from_payload({"five_hour": {"utilization": 0, "resets_at": None}, "seven_day": None})
# session clockless-0%% is fresh, so this stays available on the session alone; only true all-null closes:
check("clockless session + absent weekly ⇒ gate session ⇒ available", p.available, True)

# --- a clockless window with REAL usage must stay conservative (not silently unlock spend) --------
p = q._claude_from_payload({"five_hour": {"utilization": 80, "resets_at": None}, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
sess = next(w for w in p.windows if w.name == "session")
check("clockless 80%% session ⇒ over-pace, blocked", (sess.status, p.available), ("over-pace", False))

p = q._claude_from_payload({"five_hour": {"utilization": 100, "resets_at": None}, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
sess = next(w for w in p.windows if w.name == "session")
check("clockless 100%% session ⇒ exhausted, blocked", (sess.status, p.available), ("exhausted", False))

# --- present-but-garbage still fails closed (NOT treated as absent/fresh) ---------------------------
# A NaN usage with a real reset clock is corrupt telemetry, not a just-reset window: it must stay
# 'unknown' (used is not None ⇒ not 'absent') and fail closed, never be skipped or read as fresh.
p = q._claude_from_payload({"five_hour": {"utilization": float("nan"), "resets_at": FUTURE}, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
sess = next(w for w in p.windows if w.name == "session")
check("garbage (NaN) session ⇒ unknown, blocked", (sess.status, p.available), ("unknown", False))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
