#!/usr/bin/env python3
"""gh_run waits out a GitHub rate limit and retries, rather than failing the round and discarding the
agent's (expensive) work.

A 403 mid-round used to fail the round outright — and, for a review, count against the review-ERROR cap
that escalates a healthy PR to a human (the #302 false-escalation in the field log). Now a rate-limited
`gh` call sleeps until the limit clears and retries in place, bounded by GH_INROUND_WAIT so it can't blow
ROUND_TIMEOUT; the loop-level preflight (cmd_loop) waits out the longer hourly primary reset. This harness
pins gh_run's decisions without touching the live API: a scripted FakeRun replays (returncode, stderr)
tuples and a stubbed sleep records the waits.

Exit 0 = all cases agree; 1 = a mismatch.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec)
sys.modules["tauceti"] = tc
spec.loader.exec_module(tc)

import subprocess

PRIMARY = "HTTP 403: API rate limit exceeded for user ID 477956"
SECONDARY = "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'BAD'}] {name}: got {got!r} want {want!r}")


class FakeRun:
    """Replays a scripted list of (returncode, stderr) for each tc.run() call; records argv seen."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc, err = self.script.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr=err)


def with_stubs(run_script, budget=None):
    """Install a FakeRun and a sleep recorder; budget stubs github_budget() (so primary waits are
    deterministic without a real rate_limit probe). Returns (FakeRun, slept-list)."""
    fr = FakeRun(run_script)
    slept = []
    tc.run = fr
    tc.time.sleep = lambda s: slept.append(s)
    tc.github_budget = (lambda: budget)
    return fr, slept


def main() -> int:
    orig_run, orig_sleep, orig_budget = tc.run, tc.time.sleep, tc.github_budget

    # Classification: secondary first (its text also contains "rate limit").
    check("classify primary", tc._gh_rate_kind(PRIMARY), "primary")
    check("classify secondary", tc._gh_rate_kind(SECONDARY), "secondary")
    check("classify non-limit", tc._gh_rate_kind("HTTP 404: Not Found"), None)

    # Secondary limit clears on the second try: one wait of GH_SECONDARY_BASE, then success.
    fr, slept = with_stubs([(1, SECONDARY), (0, "")])
    p = tc.gh_run(["gh", "pr", "list"])
    check("secondary retries to success rc", p.returncode, 0)
    check("secondary waited once", slept, [tc.GH_SECONDARY_BASE])
    check("secondary made two gh calls", len(fr.calls), 2)

    # Non-rate-limit failure passes straight through, no retry, no sleep.
    fr, slept = with_stubs([(1, "HTTP 404: Not Found")])
    p = tc.gh_run(["gh", "pr", "view", "9"])
    check("non-limit returns the failure", p.returncode, 1)
    check("non-limit did not sleep", slept, [])
    check("non-limit made one call", len(fr.calls), 1)

    # Primary limit: wait is (reset - now + 5), clamped to GH_INROUND_WAIT. reset 100s out -> ~105s.
    now = int(tc.time.time())
    fr, slept = with_stubs([(1, PRIMARY), (0, "")], budget=(0, now + 100))
    p = tc.gh_run(["gh", "pr", "list"])
    check("primary retries to success", p.returncode, 0)
    check("primary waited ~until reset", 100 <= slept[0] <= 110, True)

    # Give-up: a reset further out than the in-round budget surfaces the error rather than waiting past it.
    fr, slept = with_stubs([(1, PRIMARY)], budget=(0, now + 10_000))
    p = tc.gh_run(["gh", "pr", "list"], max_wait=120)
    check("primary over-budget surfaces error", p.returncode, 1)
    check("primary over-budget did not sleep", slept, [])

    # github_budget parses the rate_limit JSON array (restore the real fn — with_stubs stubbed it).
    tc.github_budget = orig_budget

    class BudgetRun:
        def __call__(self, argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="[4321, 1750000000]", stderr="")
    tc.run = BudgetRun()
    check("github_budget parses", tc.github_budget(), (4321, 1750000000))

    tc.run, tc.time.sleep, tc.github_budget = orig_run, orig_sleep, orig_budget
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
