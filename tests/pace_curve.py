#!/usr/bin/env python3
"""The pacer's under/over-pace threshold is a piecewise-linear budget curve from $TAUCETI_PACE
(--pace), defaulting to `60:40`. Verify parsing (incl. endpoint fill and rejection of bad specs),
interpolation, and that _classify_window actually honours the curve. Dependency-free; no network."""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    fails += not cond


# --- parse_pace_curve: shape + endpoint fill --------------------------------------------------------
DEFAULT = [(0.0, 0.0), (60.0, 40.0), (100.0, 100.0)]  # the shipped default, spelled out
check("the default curve is 60:40", tc._DEFAULT_PACE == DEFAULT)
check("empty -> default curve", tc.parse_pace_curve("") == DEFAULT)
check("None -> default curve", tc.parse_pace_curve(None) == DEFAULT)
check(
    "fills both endpoints (0->0, 100->100)",
    tc.parse_pace_curve("50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)],
)
check(
    "keeps operator endpoints when given",
    tc.parse_pace_curve("0:10,50:70,90:90") == [(0.0, 10.0), (50.0, 70.0), (90.0, 90.0), (100.0, 100.0)],
)
check("sorts out-of-order points", tc.parse_pace_curve("90:90,0:10") == [(0.0, 10.0), (90.0, 90.0), (100.0, 100.0)])
check("budget may exceed 100", tc.parse_pace_curve("100:250") == [(0.0, 0.0), (100.0, 250.0)])
check("dup time same budget is fine", tc.parse_pace_curve("50:70,50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])


def rejects(spec):
    try:
        tc.parse_pace_curve(spec)
        return False
    except ValueError:
        return True


for bad in ("abc", "50", "50:", ":70", "150:10", "-5:10", "50:-1", "50:70,50:80", "50:x"):
    check(f"rejects {bad!r}", rejects(bad))
# non-finite values slip through float() but must be rejected (they'd unlock all spend / make NaN budgets)
for bad in ("50:inf", "50:nan", "50:1e999", "nan:50", "inf:50", "50:-inf"):
    check(f"rejects non-finite {bad!r}", rejects(bad))
# a spec with real (comma) structure but no points is a typo, not a request for the default
for bad in (",", ",,", ", ,"):
    check(f"rejects empty-but-nonblank {bad!r}", rejects(bad))
# but a genuinely blank/whitespace spec is 'unset' = the default, and stray commas around real points are fine
check("blank -> default curve", tc.parse_pace_curve("   ") == DEFAULT)
check("tolerates stray commas", tc.parse_pace_curve("50:70,") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])


# --- pace_budget: interpolation ---------------------------------------------------------------------
c = tc.parse_pace_curve("0:10,50:70,90:90")
check("budget at a control point", tc.pace_budget(c, 50) == 70.0)
check("budget interpolated midway 0..50", tc.pace_budget(c, 25) == 40.0)  # 10 + (25/50)*(70-10)
check("budget interpolated 50..90", tc.pace_budget(c, 70) == 80.0)  # 70 + (20/40)*(90-70)
check("budget clamps below 0", tc.pace_budget(c, -10) == 10.0)  # e clamped to 0 -> 10
check("budget clamps above 100", tc.pace_budget(c, 200) == 100.0)
ident = tc.parse_pace_curve("0:0,100:100")
check("identity budget == elapsed", tc.pace_budget(ident, 37) == 37.0)

# The default curve: two thirds of clock rate to the 60% mark, then a ramp to the full quota. It sits
# strictly below the identity line everywhere in between, so switching to it can only tighten pacing.
d = tc.parse_pace_curve("")
check("default at the knee", tc.pace_budget(d, 60) == 40.0)
check("default before the knee", tc.pace_budget(d, 30) == 20.0)
check("default after the knee", tc.pace_budget(d, 80) == 70.0)  # 40 + (20/40)*(100-40)
check("default reaches the full quota", tc.pace_budget(d, 100) == 100.0)
check("default is below identity everywhere between", all(tc.pace_budget(d, e) < e for e in range(1, 100)))


# --- pace_curve: what the LIVE env resolves to -------------------------------------------------------
# The CLI rejects a malformed spec up front, so this is the guard for a path that got past it. It must
# resolve to the default rather than raising inside the pacer or unlocking spend.
def live(spec):
    if spec is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = spec
    return tc.pace_curve()


check("unset env -> default curve", live(None) == DEFAULT)
check("empty env -> default curve", live("") == DEFAULT)
check("malformed env -> default curve, not a raise", live("0:0,90:0,100:oops") == DEFAULT)
check("valid env is honoured", live("50:70") == [(0.0, 0.0), (50.0, 70.0), (100.0, 100.0)])
os.environ.pop("TAUCETI_PACE", None)


# --- _classify_window honours the live curve --------------------------------------------------------
def status(used, elapsed, pace):
    if pace is None:
        os.environ.pop("TAUCETI_PACE", None)
    else:
        os.environ["TAUCETI_PACE"] = pace
    return tc._classify_window("session", used, elapsed, None, False).status


check("default: used 40 @ elapsed 50 -> over-pace", status(40, 50, None) == "over-pace")  # budget 33.3
check("default: used 30 @ elapsed 50 -> under-pace", status(30, 50, None) == "under-pace")
check("identity: used 60 @ elapsed 50 -> over-pace", status(60, 50, "0:0,100:100") == "over-pace")
check("identity: used 40 @ elapsed 50 -> under-pace", status(40, 50, "0:0,100:100") == "under-pace")
check("curve 50:70: used 60 @ elapsed 50 -> under-pace", status(60, 50, "50:70") == "under-pace")
check("curve 50:70: used 80 @ elapsed 50 -> over-pace", status(80, 50, "50:70") == "over-pace")
check("curve tail uncapped: used 95 @ elapsed 100 -> under-pace", status(95, 100, "90:90") == "under-pace")
check("exhausted beats the curve: used 100 @ elapsed 5 -> exhausted", status(100, 5, "0:100") == "exhausted")
os.environ["TAUCETI_PACE"] = "50:70"
w = tc._classify_window("session", 80, 50, None, False)
check("over-pace window records the budget it exceeded", w.status == "over-pace" and w.budget == 70.0)
os.environ.pop("TAUCETI_PACE", None)

# Garbage telemetry fails CLOSED (unknown), never fresh/under-pace — even under a permissive curve.
os.environ.pop("TAUCETI_PACE", None)

# --- positive headroom: under-pace means room for the NEXT request, not merely "not over" -----------
check("used < budget -> under-pace", status(49, 50, "0:50,100:50") == "under-pace")
check("used == budget -> at-budget (its own status)", status(50, 50, "0:50,100:50") == "at-budget")
check("used > budget -> over-pace", status(51, 50, "0:50,100:50") == "over-pace")
check("equality at zero is not special-cased", status(0, 0, "") == "at-budget")
check("equality at the tail too", status(90, 90, "0:0,90:90,100:90") == "at-budget")
check(
    "at-budget still records the budget it met", tc._classify_window("session", 50, 50, None, False).budget is not None
)

# --- τ₀: how long a fresh window stays completely unspendable under a curve -------------------------
# This is what decides whether a just-reset window may be INITIALIZED at all: with τ₀ > 0 the operator's
# curve says a window this fresh has no budget, and an unopened window has no clock to wait it out on.
for spec, want in (
    ("", 0.0),
    ("0:0,100:100", 0.0),
    ("0:0,100:95", 0.0),
    ("0:10,100:100", 0.0),
    ("0:0,90:0,100:95", 90.0),
    ("0:0,50:0,100:0", 100.0),
):
    got = tc.pace_zero_plateau(tc.parse_pace_curve(spec))
    check(f"tau0({spec or 'default'}) == {want}", got == want)

os.environ["TAUCETI_PACE"] = "0:100"
check("NaN elapsed -> unknown", tc._classify_window("session", 50, float("nan"), None, False).status == "unknown")
check("inf used -> unknown", tc._classify_window("session", float("inf"), 50, None, False).status == "unknown")
check("bool used -> unknown", tc._classify_window("session", True, 50, None, False).status == "unknown")
check(
    "limit_reached still exhausts despite NaN elapsed",
    tc._classify_window("session", 5, float("nan"), None, True).status == "exhausted",
)
os.environ.pop("TAUCETI_PACE", None)
print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
