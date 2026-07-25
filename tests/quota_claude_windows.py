#!/usr/bin/env python3
"""The Claude usage reader must preserve the raw epistemic state of EACH quota window before pacing.

Anthropic reports two gating windows (a 5-hour session and an unscoped weekly) in two overlapping
representations: a structured `limits` array and the legacy flat `five_hour` / `seven_day` keys. Each
window can be:

  active     a finite usage% AND a valid, unelapsed reset clock — the only state that paces;
  idle       a recognized post-reset gap (the window rolled, nothing has opened the next cycle);
  absent     no record for that window anywhere in the payload;
  malformed  a record that exists but is contradictory / non-finite / invalid / unrecognized.

The properties under test are architectural, not cosmetic:
  - the two windows reset on independent clocks, so NEITHER may be inferred from the other;
  - `absent` and `malformed` fail closed — schema drift must never silently delete a hard quota
    constraint by dropping a window from the gating set;
  - an explicit null, an omitted field and an INVALID value are three different statements (an invalid
    reset timestamp used to collapse into the same parsed None as an explicit null, and read as a
    fresh window);
  - the diagnosis names the window and the offending condition, not "usage unknown".

Dependency-free; no network.
"""

import os
import sys
import time
from datetime import UTC, datetime
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


# _claude_from_payload is the pure parse: no cache, no ledger, no bootstrap. It reaches self only for
# _next_eligible (a staticmethod), so a bare instance is enough.
q = tc.Quota.__new__(tc.Quota)

os.environ.pop("TAUCETI_PACE", None)  # legacy identity curve ⇒ budget(0)==0, so usage>0 is over pace at elapsed 0

FUTURE = "2099-01-01T00:00:00Z"  # remaining >> window ⇒ elapsed clamps to 0 ⇒ used 0 is under pace


def iso(delta_s: float) -> str:
    """An ISO reset clock delta_s seconds from now (negative = already elapsed)."""
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


def state(payload, window):
    return tc._claude_window_reading(payload, window).state


# --- the per-record truth table -------------------------------------------------------------------
# usage x reset, over {a usable value, an explicit null, an omitted field, garbage}. `idle` requires an
# EXPLICIT statement that the window is not open; silence (both fields omitted) is unrecognized, not
# idle, so drift can't turn a hard constraint into a free pass.
table = [
    ("value+clock ⇒ active", {"utilization": 10, "resets_at": FUTURE}, tc.STATE_ACTIVE),
    ("value+null clock, 0%% ⇒ idle", {"utilization": 0, "resets_at": None}, tc.STATE_IDLE),
    ("value+no clock field, 0%% ⇒ idle", {"utilization": 0}, tc.STATE_IDLE),
    ("value+null clock, 80%% ⇒ malformed", {"utilization": 80, "resets_at": None}, tc.STATE_MALFORMED),
    ("value+INVALID clock ⇒ malformed", {"utilization": 10, "resets_at": "not-a-date"}, tc.STATE_MALFORMED),
    ("value+numeric clock ⇒ malformed", {"utilization": 10, "resets_at": 1893456000}, tc.STATE_MALFORMED),
    ("null usage+null clock ⇒ idle", {"utilization": None, "resets_at": None}, tc.STATE_IDLE),
    ("null usage+no clock field ⇒ idle", {"utilization": None}, tc.STATE_IDLE),
    ("no usage field+null clock ⇒ idle", {"resets_at": None}, tc.STATE_IDLE),
    ("null usage+live clock ⇒ malformed", {"utilization": None, "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("no usage field+live clock ⇒ malformed", {"resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("nothing stated at all ⇒ malformed", {}, tc.STATE_MALFORMED),
    ("NaN usage ⇒ malformed", {"utilization": float("nan"), "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("inf usage ⇒ malformed", {"utilization": float("inf"), "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("string usage ⇒ malformed", {"utilization": "10", "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("bool usage ⇒ malformed", {"utilization": True, "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("negative usage ⇒ malformed", {"utilization": -1, "resets_at": FUTURE}, tc.STATE_MALFORMED),
    ("elapsed clock ⇒ idle (window rolled)", {"utilization": 90, "resets_at": iso(-600)}, tc.STATE_IDLE),
    ("clock about to roll ⇒ still active", {"utilization": 90, "resets_at": iso(5)}, tc.STATE_ACTIVE),
]
for name, record, want in table:
    check(name, state({"five_hour": record}, "session"), want)

# The record itself, before its fields: an explicit null IS a record ("this window is not open"); a
# missing key is the payload not mentioning the window at all; a non-object is garbage.
check("explicit null record ⇒ idle", state({"five_hour": None}, "session"), tc.STATE_IDLE)
check("key absent ⇒ absent", state({"seven_day": None}, "session"), tc.STATE_ABSENT)
check("non-object record ⇒ malformed", state({"five_hour": 3}, "session"), tc.STATE_MALFORMED)
check("list record ⇒ malformed", state({"five_hour": []}, "session"), tc.STATE_MALFORMED)

# A null reset timestamp and an INVALID one must not land in the same place.
check(
    "null vs invalid reset timestamps stay distinguishable",
    (
        state({"five_hour": {"utilization": None, "resets_at": None}}, "session"),
        state({"five_hour": {"utilization": None, "resets_at": "2026-13-45"}}, "session"),
    ),
    (tc.STATE_IDLE, tc.STATE_MALFORMED),
)

# --- the two windows are read independently -------------------------------------------------------
# A session reset must not change how the weekly reads, and vice versa: they roll on separate clocks,
# so keying either window's legitimacy on its sibling re-breaks every time the sibling rolls.
p = q._claude_from_payload({"five_hour": None, "seven_day": {"utilization": 20, "resets_at": FUTURE}})
check(
    "session reset, weekly active: states",
    [(w.name, w.status) for w in p.windows],
    [("session", tc.STATE_IDLE), ("weekly", "over-pace")],
)
check("session reset, weekly active: not available", p.available, False)

p = q._claude_from_payload({"five_hour": {"utilization": 0, "resets_at": FUTURE}, "seven_day": None})
check(
    "weekly reset, session active: states",
    [(w.name, w.status) for w in p.windows],
    [("session", "under-pace"), ("weekly", tc.STATE_IDLE)],
)
check("weekly reset, session active: not available", p.available, False)

# An idle window NEVER grants availability, even when the sibling is comfortably under pace: missing
# telemetry is not permission to spend. (The previous fix dropped an idle window from the gating set,
# so a reset window read as "no constraint" and a full round launched on it.)
p = q._claude_from_payload({"five_hour": None, "seven_day": {"utilization": 0, "resets_at": FUTURE}})
check("idle session + healthy weekly ⇒ still blocked", (p.available, p.model), (False, None))
check("idle session stays in the gating set", len(p.windows), 2)

# --- per-window choice of representation ----------------------------------------------------------
# The `limits` array and the flat keys are parsed per window, and the better reading wins for THAT
# window alone. A limits array carrying only one of the two windows must not discard the other.
mixed = {
    "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 30, "resets_at": FUTURE, "scope": None}],
    "five_hour": {"utilization": 40, "resets_at": FUTURE},
}
p = q._claude_from_payload(mixed)
check(
    "limits weekly + flat session ⇒ both windows read",
    [(w.name, w.status) for w in p.windows],
    [("session", "over-pace"), ("weekly", "over-pace")],
)
check("limits weekly + flat session ⇒ both usages parsed", [w.used for w in p.windows], [40.0, 30.0])

only_session = {
    "limits": [{"group": "session", "percent": 5, "resets_at": FUTURE}],
    "seven_day": {"utilization": 7, "resets_at": FUTURE},
}
check(
    "limits session + flat weekly ⇒ both usages parsed",
    [w.used for w in q._claude_from_payload(only_session).windows],
    [5.0, 7.0],
)

# A window missing from BOTH representations is absent — fail closed, never dropped.
p = q._claude_from_payload({"limits": [{"group": "session", "percent": 5, "resets_at": FUTURE}]})
check(
    "weekly in neither representation ⇒ absent, blocked", (p.windows[1].status, p.available), (tc.STATE_ABSENT, False)
)
check("absent weekly names itself", tc._unavail_reason(p), (False, "weekly limit missing from usage response"))

# Preference: an active reading beats a non-active one whichever representation carries it.
p = q._claude_from_payload(
    {
        "limits": [
            {"group": "session", "percent": None, "resets_at": None},
            {"group": "weekly", "percent": 0, "resets_at": FUTURE},
        ],
        "five_hour": {"utilization": 0, "resets_at": FUTURE},
    }
)
check("flat active beats an idle limits entry", (p.windows[0].status, p.available), ("under-pace", True))

p = q._claude_from_payload(
    {
        "limits": [
            {"group": "session", "percent": "garbage", "resets_at": FUTURE},
            {"group": "weekly", "percent": 0, "resets_at": FUTURE},
        ],
        "five_hour": {"utilization": 0, "resets_at": FUTURE},
    }
)
check("flat active beats a malformed limits entry", (p.windows[0].status, p.available), ("under-pace", True))

# ...but a malformed reading is only ever displaced by a BETTER one, never by silence.
p = q._claude_from_payload(
    {
        "limits": [
            {"group": "session", "percent": "garbage", "resets_at": FUTURE},
            {"group": "weekly", "percent": 0, "resets_at": FUTURE},
        ]
    }
)
check(
    "malformed limits + no flat fallback ⇒ malformed, blocked",
    (p.windows[0].status, p.available),
    (tc.STATE_MALFORMED, False),
)

# --- gating: the scoped weekly caps never gate, both real windows always do ------------------------
full = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 0, "resets_at": FUTURE, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 0, "resets_at": FUTURE, "scope": None},
        # a per-model weekly cap, maxed out — must be ignored, never gate opus:
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 99,
            "resets_at": None,
            "scope": {"model": {"display_name": "Fable"}},
        },
    ],
    "five_hour": None,
    "seven_day": None,
    "seven_day_sonnet": None,
}
p = q._claude_from_payload(full)
check("limits: exactly two gating windows (no sonnet/scoped)", [w.name for w in p.windows], ["session", "weekly"])
check("limits: both under pace ⇒ available opus", (p.available, p.model), (True, "opus"))
check("limits: a maxed per-model (scoped) weekly does NOT block opus", p.available, True)

spent = {
    **full,
    "limits": [
        {"group": "session", "percent": 0, "resets_at": FUTURE, "scope": None},
        {"group": "weekly", "percent": 100, "resets_at": FUTURE, "scope": None},
    ],
}
p = q._claude_from_payload(spent)
check(
    "weekly at 100%% ⇒ exhausted ⇒ unavailable",
    (p.available, tc._unavail_reason(p)),
    (False, (False, "weekly exhausted")),
)

flat = {
    "five_hour": {"utilization": 0, "resets_at": FUTURE},
    "seven_day": {"utilization": 0, "resets_at": FUTURE},
    "seven_day_sonnet": None,  # present-but-null: must be ignored, not a third window
}
p = q._claude_from_payload(flat)
check("flat: two windows, no sonnet", [w.name for w in p.windows], ["session", "weekly"])
check("flat: available opus", (p.available, p.model), (True, "opus"))
check("empty limits array ⇒ flat still read", q._claude_from_payload({"limits": [], **flat}).available, True)
check("non-list limits ⇒ flat still read", q._claude_from_payload({"limits": {"x": 1}, **flat}).available, True)

# A genuinely active pair paces normally: half the session window elapsed, usage under the line.
mid = {
    "five_hour": {"utilization": 40, "resets_at": iso(tc.SESSION_WINDOW_S / 2)},
    "seven_day": {"utilization": 10, "resets_at": iso(tc.WEEK_WINDOW_S / 2)},
}
p = q._claude_from_payload(mid)
check("active pair under the pace line ⇒ available", (p.available, p.model), (True, "opus"))
check("active pair reports elapsed", [round(w.elapsed) for w in p.windows], [50, 50])

over = {
    "five_hour": {"utilization": 80, "resets_at": iso(tc.SESSION_WINDOW_S / 2)},
    "seven_day": {"utilization": 10, "resets_at": iso(tc.WEEK_WINDOW_S / 2)},
}
soft, why = tc._unavail_reason(q._claude_from_payload(over))
check("active pair over the pace line ⇒ SOFT block", soft, True)
check("over-pace reason keeps its detail", why, "session ahead of pace (used 80% > 50% budget), 20% left")

# --- diagnosis: name the condition, never a bare "usage unknown" -----------------------------------
live_weekly = {"utilization": 0, "resets_at": FUTURE}
diagnoses = [
    ("session window reset; awaiting initialization", {"five_hour": None, "seven_day": live_weekly}),
    ("weekly limit missing from usage response", {"five_hour": {"utilization": 0, "resets_at": FUTURE}}),
    (
        "session reset timestamp invalid (five_hour)",
        {"five_hour": {"utilization": 5, "resets_at": "nope"}, "seven_day": live_weekly},
    ),
    (
        "session usage figure is not a usable percentage (five_hour)",
        {"five_hour": {"utilization": float("nan"), "resets_at": FUTURE}, "seven_day": live_weekly},
    ),
    (
        "session usage figure missing with a live reset clock (five_hour)",
        {"five_hour": {"resets_at": FUTURE}, "seven_day": live_weekly},
    ),
    (
        "session usage 80% reported with no reset clock (five_hour)",
        {"five_hour": {"utilization": 80, "resets_at": None}, "seven_day": live_weekly},
    ),
    ("session exhausted", {"five_hour": {"utilization": 100, "resets_at": FUTURE}, "seven_day": live_weekly}),
]
for want, payload in diagnoses:
    check(f"diagnosis: {want}", tc._unavail_reason(q._claude_from_payload(payload)), (False, want))

# Every one of those is a HARD block: --ignore-quota overrides pacing, never availability.
for want, payload in diagnoses:
    verdict = tc._ignore_quota_verdict(None, q._claude_from_payload(payload))
    check(f"hard block ⇒ --ignore-quota waits ({want[:30]})", verdict, "wait")

# Both windows unreadable at once still reports both, and says the response itself is unusable.
p = q._claude_from_payload({})
check(
    "no recognizable record at all ⇒ schema error",
    (p.available, p.error),
    (False, "claude usage response schema unsupported (no session or weekly record)"),
)
check(
    "no recognizable record at all ⇒ both windows named",
    tc._unavail_reason(p),
    (False, "session limit missing from usage response; weekly limit missing from usage response"),
)

# A hard block co-occurring with an over-pace sibling stays HARD (we cannot tell the unreadable window
# is not exhausted), and the unreadable window is what gets reported.
p = q._claude_from_payload(
    {
        "five_hour": {"utilization": 60, "resets_at": iso(tc.SESSION_WINDOW_S / 2)},
        "seven_day": {"utilization": 5, "resets_at": "nope"},
    }
)
soft, why = tc._unavail_reason(p)
check("unreadable dominates a co-occurring over-pace window", soft, False)
check("unreadable window is the reported reason", why, "weekly reset timestamp invalid (seven_day)")

# --- schema drift can never delete a hard quota constraint -----------------------------------------
# Whatever we drop from or rename in a healthy payload, the result is either still fully readable or a
# hard block — never "available" on the strength of a window that stopped being reported.
healthy = {"five_hour": {"utilization": 0, "resets_at": FUTURE}, "seven_day": {"utilization": 0, "resets_at": FUTURE}}
check("baseline healthy payload is available", q._claude_from_payload(healthy).available, True)
for drop in ("five_hour", "seven_day"):
    p = q._claude_from_payload({k: v for k, v in healthy.items() if k != drop})
    check(f"dropping {drop} ⇒ blocked (constraint kept, not deleted)", (p.available, len(p.windows)), (False, 2))
for renamed in ("five_hour_v2", "session"):
    p = q._claude_from_payload({renamed: healthy["five_hour"], "seven_day": healthy["seven_day"]})
    check(f"renaming five_hour→{renamed} ⇒ blocked", p.available, False)
for key in ("utilization", "resets_at"):
    degraded = {**healthy, "five_hour": {k: v for k, v in healthy["five_hour"].items() if k != key}}
    check(f"dropping five_hour.{key} ⇒ never available", q._claude_from_payload(degraded).available, False)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
