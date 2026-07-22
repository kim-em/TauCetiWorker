#!/usr/bin/env python3
"""A host review posts its scoreboard to the PR, then publishes its records to TauCetiData with a git
push (via _sync_review_outbox). That push is MACHINE-WIDE: a stale gh credential helper, a network
blip, or the remote being down fails every PR's publish identically. do_review used to count a publish
failure as a PER-PR review error, so during a credential-helper outage every green PR marched one-by-one
to the "needs a human" cap even though each review posted fine. do_review must instead treat a publish
failure like the host-binary preflight: warn loudly and raise NoProgress (⇒ backoff, no counter bump),
leaving the records in the outbox for a later round to re-drain.

This test drives do_review with a minimal fake Worker and the engine + sync stubbed, so it needs no
network and no real store. Exit 0 = every case agrees; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

wu = tc.work_units
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


class FakeCounters:
    """Just the read/write/incr surface do_review uses, over an in-memory dict."""

    def __init__(self):
        self.d = {}

    def read(self, name):
        return self.d.get(name, 0)

    def write(self, name, value):
        self.d[name] = value

    def incr(self, name):
        self.d[name] = self.read(name) + 1
        return self.d[name]


class FakeRS:
    def __init__(self):
        self.busted = []

    def review_rounds(self, pr, counters):
        return 0

    def bust(self, pr):
        self.busted.append(pr)


class FakeGH:
    pass


class FakeCfg:
    store_dir = Path("/tmp/does-not-matter/store")
    logdir = Path("/tmp/does-not-matter/logs")


class FakeWorker:
    def __init__(self):
        self.counters = FakeCounters()
        self.rs = FakeRS()
        self.gh = FakeGH()
        self.cfg = FakeCfg()


def opts(work_model="codex"):
    return tc.RoundOpts(only=["review"], agent=work_model, work_model=work_model, sandbox_host=True, dry_run=False)


# Stub the engine (posts fine, rc=0) and the module-level helpers do_review calls on the host path.
warns = []
_saved = {k: getattr(wu, k) for k in ("run_to_logfile", "_sync_review_outbox", "warn_red", "me")}
wu.run_to_logfile = lambda argv, logf, label: 0  # the review engine posts successfully
wu.warn_red = lambda msg: warns.append(msg)
wu.me = lambda: "kim-em"

CAND = tc.Candidate(726, "deadbeef", "build-green")

try:
    # 1) engine posts, but the TauCetiData publish FAILS -> NoProgress, no per-PR counter bump, warned.
    #    The engine posted a verdict, so a prior error streak is CLEARED (reset on post, before the
    #    publish step) rather than bumped — the publish failure is machine-wide, never charged to the PR.
    wu._sync_review_outbox = lambda w, pr: 1  # push failed after retries
    w = FakeWorker()
    w.counters.write("review-err-726", 2)  # a prior genuine error streak...
    warns.clear()
    raised, msg = False, ""
    try:
        wu.do_review(w, None, CAND, opts(), bubble=False)
    except tc.NoProgress as e:
        raised, msg = True, str(e)
    check("publish fails -> NoProgress raised", raised, True)
    check("publish fails -> errkey reset on post (not bumped)", w.counters.read("review-err-726"), 0)
    check("publish fails -> warned red once", len(warns), 1)
    check("publish fails -> warning says machine-wide / not charged", "not charged" in warns[0].lower(), True)
    check("publish fails -> NoProgress msg names TauCetiData", "TauCetiData" in msg, True)
    check("publish fails -> ledger NOT busted (round did not fully land)", w.rs.busted, [])

    # 1b) reset-on-post prevents false escalation: a posted verdict (even with a failed publish) clears
    #     the streak, so a single later engine error cannot combine with pre-post errors to hit the cap.
    wu._sync_review_outbox = lambda w, pr: 1
    w = FakeWorker()
    w.counters.write("review-err-726", 2)
    try:
        wu.do_review(w, None, CAND, opts(), bubble=False)  # posts, publish fails -> streak reset to 0
    except tc.NoProgress:
        pass
    wu.run_to_logfile = lambda argv, logf, label: 4  # now the engine genuinely errors (no verdict posted)
    wu.do_review(w, None, CAND, opts(), bubble=False)
    check("reset-on-post -> later engine error starts a fresh streak (1, not 3)", w.counters.read("review-err-726"), 1)
    wu.run_to_logfile = lambda argv, logf, label: 0  # restore for the cases below

    # 2) engine posts and the publish SUCCEEDS -> errkey reset to 0, ledger busted, no warning.
    wu._sync_review_outbox = lambda w, pr: 0
    w = FakeWorker()
    w.counters.write("review-err-726", 2)
    warns.clear()
    rc = wu.do_review(w, None, CAND, opts(), bubble=False)
    check("publish ok -> rc 0", rc, 0)
    check("publish ok -> errkey reset to 0", w.counters.read("review-err-726"), 0)
    check("publish ok -> ledger busted", w.rs.busted, [726])
    check("publish ok -> no red warning", len(warns), 0)

    # 3) the engine ITSELF errors (rc!=0) -> that IS per-PR, so the errkey is bumped (unchanged behaviour).
    wu.run_to_logfile = lambda argv, logf, label: 3
    wu._sync_review_outbox = lambda w, pr: 0
    w = FakeWorker()
    w.counters.write("review-err-726", 2)
    warns.clear()
    rc = wu.do_review(w, None, CAND, opts(), bubble=False)
    check("engine errors -> rc propagated", rc, 3)
    check("engine errors -> errkey bumped to 3", w.counters.read("review-err-726"), 3)
finally:
    for k, v in _saved.items():
        setattr(wu, k, v)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
