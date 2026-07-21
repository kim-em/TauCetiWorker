#!/usr/bin/env python3
"""Regression guard for PRInfo.from_json's build signal.

`statusCheckRollup` mixes two node types: CheckRun (carries `name`/`conclusion`) and StatusContext
(carries `context`/`state`). The required `build` result the merge gate reads is a commit STATUS
(context=="build"). On a FORK PR the rollup ALSO has a check-run named "build" — the
pull_request_target job, which can fail (e.g. the actions/checkout fork-refusal step) or finish
without the trusted sandboxed build ever running. Reading only name=="build" saw that check-run and
missed the passing status, so a green, mergeable fork PR looked red: routed to fix-ci, never reviewed,
CI-budget exhausted, wedged behind roadmap backpressure.

The rules this locks in:
  - the commit status (context=="build") is authoritative whenever present;
  - the `build` check-run is trusted ONLY as a fallback for a SAME-REPO PR (head in the base repo),
    where it IS the real build — never for a fork/cross-repo PR, which waits for the status instead;
  - build_success and build_failed are mutually exclusive and never invent a verdict from nothing.

Exit 0 = all cases classify correctly; 1 = a mismatch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tauceti_worker as tc

SAME = tc.TAUCETI_OWNER  # head in the base repo → the `build` check-run is the real build
FORK = "some-fork-owner"  # head in a fork → the `build` check-run is untrusted noise


def checkrun(conclusion):
    return {"__typename": "CheckRun", "name": "build", "conclusion": conclusion}


def status(state):
    return {"__typename": "StatusContext", "context": "build", "state": state}


def pr(rollup, owner):
    return tc.PRInfo.from_json({"number": 1, "headRepositoryOwner": {"login": owner}, "statusCheckRollup": rollup})


# (label, rollup, owner, want_success, want_failed)
CASES = [
    # The wedge: a green fork PR — the fork-checkout check-run FAILS, the trusted build status SUCCEEDS.
    # The status is authoritative → reviewable, not a fix-ci candidate.
    ("false-red fork (checkrun FAIL + status SUCCESS)", [checkrun("FAILURE"), status("SUCCESS")], FORK, True, False),
    # A genuinely red fork PR — both the check-run and the trusted status fail.
    ("truly red fork (checkrun FAIL + status FAILURE)", [checkrun("FAILURE"), status("FAILURE")], FORK, False, True),
    # order must not matter (rollup ordering is not guaranteed).
    ("false-red fork, status listed first", [status("SUCCESS"), checkrun("FAILURE")], FORK, True, False),
    # Same-repo PR that posts only a check-run (no status yet) — fall back to the check-run.
    ("same-repo checkrun-only SUCCESS (fallback)", [checkrun("SUCCESS")], SAME, True, False),
    ("same-repo checkrun-only FAILURE (fallback)", [checkrun("FAILURE")], SAME, False, True),
    # The fork gate: a fork PR with ONLY a `build` check-run (no status) must trust NEITHER — it waits
    # for the trusted build to post, rather than being routed to fix-ci (or review) on check-run noise.
    ("fork checkrun-only FAILURE → wait", [checkrun("FAILURE")], FORK, False, False),
    ("fork checkrun-only SUCCESS → wait", [checkrun("SUCCESS")], FORK, False, False),
    # A present status (even PENDING) is authoritative and suppresses the fork check-run entirely.
    ("fork status PENDING + checkrun FAILURE → wait", [status("PENDING"), checkrun("FAILURE")], FORK, False, False),
    # Status-only, both polarities.
    ("status-only SUCCESS", [status("SUCCESS")], FORK, True, False),
    ("status-only FAILURE", [status("FAILURE")], FORK, False, True),
    # A pending / expected build status is neither success nor failure (the PR simply waits).
    ("status PENDING", [status("PENDING")], FORK, False, False),
    ("status EXPECTED", [status("EXPECTED")], FORK, False, False),
    # Mixed chosen states within the authoritative domain: failure wins, success needs unanimity.
    ("two statuses SUCCESS + FAILURE → failed", [status("SUCCESS"), status("FAILURE")], FORK, False, True),
    ("two statuses SUCCESS + PENDING → wait", [status("SUCCESS"), status("PENDING")], FORK, False, False),
    ("same-repo checkruns SUCCESS + FAILURE → failed", [checkrun("SUCCESS"), checkrun("FAILURE")], SAME, False, True),
    ("duplicate status SUCCESS → success", [status("SUCCESS"), status("SUCCESS")], FORK, True, False),
    # No build signal at all → neither (don't invent a pass or a fail).
    ("no build entries", [{"__typename": "CheckRun", "name": "label", "conclusion": "SUCCESS"}], SAME, False, False),
    ("empty rollup", [], SAME, False, False),
    # A non-"FAILURE" terminal build state still counts as failed (matches BUILD_FAIL).
    ("status ERROR is failed", [status("ERROR")], FORK, False, True),
]


def main():
    fails = 0
    for label, rollup, owner, want_s, want_f in CASES:
        p = pr(rollup, owner)
        ok = p.build_success == want_s and p.build_failed == want_f
        flag = "OK " if ok else "XX "
        print(
            f"[{flag}] {label:52} success={p.build_success!s:5} failed={p.build_failed!s:5} "
            f"(want success={want_s!s:5} failed={want_f})"
        )
        if not ok:
            fails += 1
    # build_success and build_failed must never both be true.
    for label, rollup, owner, *_ in CASES:
        p = pr(rollup, owner)
        if p.build_success and p.build_failed:
            print(f"[XX ] {label}: build_success AND build_failed both true")
            fails += 1
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} case mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
