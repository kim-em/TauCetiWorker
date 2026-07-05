#!/usr/bin/env python3
"""A host round shells out to `codex`/`claude`/`pi`. If that binary slips off the worker's PATH (an npm
reinstall relocating codex is the case that bit us), the review engine rejects `--reviewer codex` and
do_review counts it as a PER-PR review error — so a machine-wide outage marches PRs one-by-one to the
"needs a human" escalation cap. dispatch() must preflight the binary and, when it is missing, pause the
round as NoProgress (⇒ backoff, no counter bump) rather than launch a doomed stage.

This test drives dispatch() with the do_* stage stubbed out, so it needs no real Worker: the preflight
raises before any stage work. Exit 0 = every case agrees; 1 = a mismatch.
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


# --- the model -> host-binary map (mirrors the launchers in agents.py + the engine's which() gate) ---
check("codex -> codex", wu._host_agent_binary("codex"), "codex")
check("claude -> claude", wu._host_agent_binary("claude"), "claude")
check("deepseek -> pi", wu._host_agent_binary("deepseek"), "pi")
check("minimax -> pi", wu._host_agent_binary("minimax"), "pi")
check("auto -> None (nothing to gate)", wu._host_agent_binary("auto"), None)
check("unknown -> None (nothing to gate)", wu._host_agent_binary("nope"), None)


# --- dispatch() preflight -----------------------------------------------------------------------
CAND = tc.Candidate(726, "deadbeef", "test")


def opts(work_model="codex", sandbox_host=True):
    return tc.RoundOpts(
        only=["review"], agent=work_model, work_model=work_model, sandbox_host=sandbox_host, dry_run=False
    )


# Stubs so dispatch needs no real Worker: the stage fn is replaced, and the "nothing landed" progress
# guard is neutralised. _bubble is pinned per-case to isolate the host/bubble scoping from its own logic.
runs = {"n": 0}
warns = []


def fake_review(w, sv, c, o, bubble):
    runs["n"] += 1
    return 0


_saved = {k: getattr(wu, k) for k in ("do_review", "warn_red", "_progress_snapshot", "_progressed", "_bubble")}
_saved_which = wu.shutil.which
wu.do_review = fake_review
wu.warn_red = lambda msg: warns.append(msg)
wu._progress_snapshot = lambda w, c: None
wu._progressed = lambda w, c, pre: True  # stub out the "nothing landed on GitHub" guard


def reset():
    runs["n"] = 0
    warns.clear()


try:
    # 1) host + binary MISSING -> pause as NoProgress, do NOT run the stage (no per-PR counter bump).
    wu._bubble = lambda stage, o: False
    wu.shutil.which = lambda name: None
    reset()
    raised, msg = False, ""
    try:
        wu.dispatch("review", None, None, CAND, opts())
    except tc.NoProgress as e:
        raised, msg = True, str(e)
    check("missing codex -> NoProgress", raised, True)
    check("missing codex -> stage NOT run (no counter bump)", runs["n"], 0)
    check("missing codex -> warned red once", len(warns), 1)
    check("NoProgress names the binary + PATH", ("codex" in msg and "PATH" in msg), True)

    # 2) host + binary PRESENT -> proceeds to the stage, no warning.
    wu.shutil.which = lambda name: f"/usr/bin/{name}"
    reset()
    rc = wu.dispatch("review", None, None, CAND, opts())
    check("present codex -> stage runs", runs["n"], 1)
    check("present codex -> rc from stage", rc, 0)
    check("present codex -> no red warning", len(warns), 0)

    # 3) bubble path -> host preflight is skipped even when the host binary is missing (the binary lives
    #    inside the container, so a host which() is not the right gate there).
    wu._bubble = lambda stage, o: True
    wu.shutil.which = lambda name: None
    reset()
    rc = wu.dispatch("review", None, None, CAND, opts(sandbox_host=False))
    check("bubble -> host preflight skipped, stage runs", runs["n"], 1)
    check("bubble -> no red warning", len(warns), 0)
finally:
    for k, v in _saved.items():
        setattr(wu, k, v)
    wu.shutil.which = _saved_which

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
