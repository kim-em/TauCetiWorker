#!/usr/bin/env python3
"""Unrestricted work tends the worker's own PRs before general reviews."""

import sys
from pathlib import Path

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
    check("single shared auto order", tc.AUTO_STAGES, ("rebase", "fix-ci", "fix", "review", "bump")),
]

sv.red_ci.actionable.append(candidate)
checks.append(check("red CI beats review findings", tc._next_auto_stage(sv), "fix-ci"))

sv.rebaseable.actionable.append(candidate)
checks.append(check("conflict remains first", tc._next_auto_stage(sv), "rebase"))

sys.exit(0 if all(checks) else 1)
