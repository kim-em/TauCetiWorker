#!/usr/bin/env python3
"""The worker must skip a PR that is already at the engine's per-PR daily review cap, BEFORE selecting it
and launching the engine (which clones repos then refuses). Otherwise it tight-loops re-reviewing a capped
PR every ~45s — the failure that buried the queue after an outage made every round error and hit 12/12.

`_review_rounds_today` mirrors the engine's count (review.py): rounds in the LOCAL ledger whose `ts` is
today (UTC). It returns 0 when the ledger/PR is simply absent (a never-reviewed PR is reviewable) and a
fail-CLOSED None when the ledger exists but can't be parsed (a torn file must not silently re-enable the
loop). The survey-time rule skips when the count is >= cap OR is the None sentinel.

Exit 0 = all cases agree; 1 = a mismatch.
"""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec)
sys.modules["tauceti"] = tc
spec.loader.exec_module(tc)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YEST = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
CAP = tc.REVIEW_DAILY_CAP

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def store_with(rounds) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "ledger.json").write_text(json.dumps({"prs": {"306": {"rounds": rounds}}}))
    return d


def skipped(count) -> bool:
    """Mirror the survey-time decision in tauceti: skip when capped or fail-closed."""
    return count is None or count >= CAP


# Counting: today's rounds only; yesterday's are ignored (the cap resets at 00:00 UTC).
mixed = [{"ts": f"{TODAY}T{h:02d}:00:00+00:00"} for h in range(CAP)] + [{"ts": f"{YEST}T0{i}:00:00+00:00"} for i in range(3)]
d = store_with(mixed)
check("counts exactly today's rounds (ignores yesterday)", tc._review_rounds_today(d, 306), CAP)
check("at cap -> skipped", skipped(tc._review_rounds_today(d, 306)), True)

# One under the cap -> reviewable.
d = store_with([{"ts": f"{TODAY}T{h:02d}:00:00+00:00"} for h in range(CAP - 1)])
check("one under cap counts correctly", tc._review_rounds_today(d, 306), CAP - 1)
check("under cap -> NOT skipped", skipped(tc._review_rounds_today(d, 306)), False)

# Missing ledger / unseen PR -> 0 (a never-reviewed PR is reviewable).
empty = Path(tempfile.mkdtemp())
check("missing ledger -> 0", tc._review_rounds_today(empty, 306), 0)
check("missing ledger -> NOT skipped", skipped(tc._review_rounds_today(empty, 306)), False)
d = store_with([{"ts": f"{TODAY}T01:00:00+00:00"}])
check("PR absent from ledger -> 0", tc._review_rounds_today(d, 999), 0)

# Corrupt ledger -> None sentinel -> fail CLOSED (skip), never a silent re-enable of the loop.
bad = Path(tempfile.mkdtemp())
(bad / "ledger.json").write_text("{ this is not json")
check("corrupt ledger -> None (fail-closed sentinel)", tc._review_rounds_today(bad, 306), None)
check("corrupt ledger -> skipped", skipped(tc._review_rounds_today(bad, 306)), True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
