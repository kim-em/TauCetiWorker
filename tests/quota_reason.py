#!/usr/bin/env python3
"""quota_line must tell the truth about *why* a provider is unavailable.

An over-pace window means we're pacing the burn with real quota left (a soft block, yellow ~), which is
NOT the same as an exhausted or unknown window (a hard block, red ✗). The old code printed a single
"over-pace/exhausted" reason and a red ✗ for both, so a healthy worker that was merely ahead of pace
looked out of quota. The `weekly_sonnet` window never gates opus, so it must not be reported. Dependency-free.
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


def W(name, used, elapsed, status):
    return tc.Window(name, used, elapsed, None, status)


def prov(*windows):
    return tc.Provider("claude", False, None, list(windows))


# --- _unavail_reason: (soft, reason) ----------------------------------------
reason_cases = [
    ("over-pace is a soft block reporting headroom",
     prov(W("session", 36.0, 23.0, "over-pace"), W("weekly", 32.0, 86.0, "under-pace")),
     (True, "session ahead of pace, 64% left")),
    ("exhausted is a hard block",
     prov(W("session", 100.0, 50.0, "exhausted"), W("weekly", 20.0, 80.0, "under-pace")),
     (False, "session exhausted")),
    ("exhausted dominates a co-occurring over-pace window",
     prov(W("session", 100.0, 50.0, "exhausted"), W("weekly", 60.0, 40.0, "over-pace")),
     (False, "session exhausted")),
    ("unknown gating window is a hard block",
     prov(W("session", None, None, "unknown"), W("weekly", 20.0, 80.0, "under-pace")),
     (False, "usage unknown")),
    ("weekly_sonnet is ignored; only the real gate is reported",
     prov(W("session", 70.0, 40.0, "over-pace"), W("weekly", 32.0, 86.0, "under-pace"),
          W("weekly_sonnet", 0.0, None, "unknown")),
     (True, "session ahead of pace, 30% left")),
    ("two over-pace windows are both listed",
     prov(W("session", 55.0, 40.0, "over-pace"), W("weekly", 48.0, 46.0, "over-pace")),
     (True, "session ahead of pace, 45% left; weekly ahead of pace, 52% left")),
]

fails = 0
for name, p, expected in reason_cases:
    got = tc._unavail_reason(p)
    ok = got == expected
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got} want={expected}")
    fails += not ok

# --- quota_line: glyph + honest reason end-to-end ---------------------------
soft = {"codex": tc.Provider("codex", False, None,
                             [W("session", 9.0, 41.0, "under-pace"), W("weekly", 48.0, 46.0, "over-pace")])}
line = tc.quota_line(soft)
for want in ("codex", "[yellow]~[/]", "weekly ahead of pace, 52% left"):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] quota_line soft contains {want!r}: {line!r}")
    fails += not ok
ok = "[red]✗[/]" not in line
print(f"[{'OK ' if ok else 'XX '}] quota_line soft is not a red block: {line!r}")
fails += not ok

hard = {"claude": tc.Provider("claude", False, None,
                              [W("session", 100.0, 30.0, "exhausted"), W("weekly", 10.0, 50.0, "under-pace")])}
line = tc.quota_line(hard)
for want in ("[red]✗[/]", "session exhausted"):
    ok = want in line
    print(f"[{'OK ' if ok else 'XX '}] quota_line hard contains {want!r}: {line!r}")
    fails += not ok

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
