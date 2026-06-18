#!/usr/bin/env python3
"""_safe_exists must degrade a permission-denied probe to False rather than raise, so `doctor` and
--isolate-home survive a walled-off ~/.codex / ~/.claude (e.g. under a sandbox or macOS data
protection). Regression test for the `doctor` crash on PermissionError from a raw Path.exists()."""
import importlib.machinery, importlib.util, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec); sys.modules["tauceti"] = tc; spec.loader.exec_module(tc)

fails = 0
def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    if not cond:
        fails += 1

# Matches Path.exists() for the ordinary cases.
check("present path -> True", tc._safe_exists(REPO / "tauceti"))
check("absent path -> False", not tc._safe_exists(REPO / "does-not-exist-xyz"))

# The whole point: a probe that raises (a walled-off ~/.codex) degrades to False, never propagates.
class _Denied:
    def exists(self):
        raise PermissionError(1, "Operation not permitted")
class _OSErr:
    def exists(self):
        raise OSError("boom")
check("PermissionError -> False (not raised)", tc._safe_exists(_Denied()) is False)
check("generic OSError -> False (not raised)", tc._safe_exists(_OSErr()) is False)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
