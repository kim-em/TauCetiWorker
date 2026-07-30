#!/usr/bin/env python3
"""Unrestricted work tends the worker's own PRs before general reviews."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc


def check(name, got, want):
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    return ok


sv = tc.Survey(worker_id="test")
candidate = tc.Candidate(1, "deadbeef", "test")
sv.reviewable.actionable.append(candidate)
sv.needs_fix.actionable.append(candidate)

checks = [
    check("fix beats unrelated review", tc._next_auto_stage(sv), "fix"),
    # `progress` is last: rate-limited to one report a day, so it cannot crowd out queue tending, and
    # ahead of roadmap authoring (handled separately after these) so a busy queue cannot defer it.
    check("single shared auto order", tc.AUTO_STAGES, ("rebase", "fix-ci", "fix", "review", "bump", "progress")),
]

# Drive the real cascade as well as its status predictor. A future edit must not let their shared
# priority drift while leaving this display-only helper green.
saved_survey = tc.work_units.survey
saved_dispatch = tc.work_units.dispatch
seen = []
tc.work_units.survey = lambda *_a, **_k: sv
tc.work_units.dispatch = lambda stage, *_a, **_k: seen.append(stage) or 0
try:
    worker = SimpleNamespace(cfg=None, gh=None, rs=None, counters=None)
    tc.work_units.run_round(worker, SimpleNamespace(only=[], dry_run=True))
finally:
    tc.work_units.survey = saved_survey
    tc.work_units.dispatch = saved_dispatch
checks.append(check("runtime cascade agrees with predictor", seen, ["fix"]))

# A due progress report must never preempt queue tending: it is cosmetic next to a red PR or a
# waiting review, and it is rate-limited anyway, so anything actionable wins.
busy = tc.Survey(worker_id="test")
busy.progress.actionable.append(tc.Candidate(0, "", "due"))
busy.reviewable.actionable.append(candidate)
checks.append(check("review beats a due progress report", tc._next_auto_stage(busy), "review"))

# But with the queue empty it must be chosen ahead of roadmap authoring, or the open-ended fallback
# (which always has work) would defer it for ever.
quiet = tc.Survey(worker_id="test")
quiet.progress.actionable.append(tc.Candidate(0, "", "due"))
checks.append(check("progress beats roadmap authoring", tc._next_auto_stage(quiet), "progress"))

# And when no report is due, the fallback is roadmap authoring as before.
idle = tc.Survey(worker_id="test")
checks.append(check("no report due falls back to roadmap", tc._next_auto_stage(idle), "roadmap"))

sv.red_ci.actionable.append(candidate)
checks.append(check("red CI beats review findings", tc._next_auto_stage(sv), "fix-ci"))

sv.rebaseable.actionable.append(candidate)
checks.append(check("conflict remains first", tc._next_auto_stage(sv), "rebase"))

sys.exit(0 if all(checks) else 1)
