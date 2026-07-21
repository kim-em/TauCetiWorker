#!/usr/bin/env python3
"""Regression guard for PRInfo.from_json's build signal.

`statusCheckRollup` mixes two node types: CheckRun (carries `name`/`conclusion`) and StatusContext
(carries `context`/`state`). The required `build` result the merge gate reads is a commit STATUS
(context=="build"). On a FORK PR the rollup ALSO has a check-run named "build" — the
pull_request_target job that always FAILS at the actions/checkout fork-refusal step. Reading only
name=="build" therefore saw the failing check-run and missed the passing status, so a green, mergeable
fork PR looked red: routed to fix-ci, never reviewed, CI-budget exhausted, wedged behind roadmap
backpressure. This locks in that the commit status wins and the noisy check-run is ignored.

Exit 0 = all cases classify correctly; 1 = a mismatch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc


def checkrun(conclusion):
    return {"__typename": "CheckRun", "name": "build", "conclusion": conclusion}


def status(state):
    return {"__typename": "StatusContext", "context": "build", "state": state}


def pr(rollup):
    return tc.PRInfo.from_json({"number": 1, "statusCheckRollup": rollup})


# (label, rollup, want_success, want_failed)
CASES = [
    # The wedge: a green fork PR — the fork-checkout check-run FAILS, the trusted build status SUCCEEDS.
    # The status is authoritative → reviewable, not a fix-ci candidate.
    ("false-red fork (checkrun FAIL + status SUCCESS)", [checkrun("FAILURE"), status("SUCCESS")], True, False),
    # A genuinely red fork PR — both the check-run and the trusted status fail.
    ("truly red fork (checkrun FAIL + status FAILURE)", [checkrun("FAILURE"), status("FAILURE")], False, True),
    # order must not matter (rollup ordering is not guaranteed).
    ("false-red, status listed first", [status("SUCCESS"), checkrun("FAILURE")], True, False),
    # Same-repo PR that posts only a check-run (no status yet) — fall back to the check-run.
    ("checkrun-only SUCCESS (fallback)", [checkrun("SUCCESS")], True, False),
    ("checkrun-only FAILURE (fallback)", [checkrun("FAILURE")], False, True),
    # Status-only, both polarities.
    ("status-only SUCCESS", [status("SUCCESS")], True, False),
    ("status-only FAILURE", [status("FAILURE")], False, True),
    # A pending build status is neither success nor failure (the PR simply waits).
    ("status PENDING", [status("PENDING")], False, False),
    # No build signal at all → neither (don't invent a pass or a fail).
    ("no build entries", [{"__typename": "CheckRun", "name": "label", "conclusion": "SUCCESS"}], False, False),
    ("empty rollup", [], False, False),
    # A non-"FAILURE" terminal build state still counts as failed (matches BUILD_FAIL).
    ("status ERROR is failed", [status("ERROR")], False, True),
]


def main():
    fails = 0
    for label, rollup, want_s, want_f in CASES:
        p = pr(rollup)
        ok = p.build_success == want_s and p.build_failed == want_f
        flag = "OK " if ok else "XX "
        print(f"[{flag}] {label:48} success={p.build_success!s:5} failed={p.build_failed!s:5} "
              f"(want success={want_s!s:5} failed={want_f})")
        if not ok:
            fails += 1
    # build_success and build_failed must never both be true.
    for label, rollup, *_ in CASES:
        p = pr(rollup)
        if p.build_success and p.build_failed:
            print(f"[XX ] {label}: build_success AND build_failed both true")
            fails += 1
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} case mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
