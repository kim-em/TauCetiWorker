#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich", "textual"]
# ///
"""Headless smoke for the Textual dashboard: build the app with a STUB survey loader (no GitHub, no
tty) and drive it with App.run_test(). Asserts the lag fix — cursor moves and dial toggles are pure
local state — plus expand/collapse and the launch follow-up. Exit 0 = all checks pass, 1 = a failure."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace

# Isolate the prefs file: persistence must land here, not in the operator's real ~/.config.
_CFGDIR = tempfile.mkdtemp(prefix="tauceti-prefs-")
os.environ["XDG_CONFIG_HOME"] = _CFGDIR

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_loader("tauceti", importlib.machinery.SourceFileLoader("tauceti", str(REPO / "tauceti")))
tc = importlib.util.module_from_spec(spec)
sys.modules["tauceti"] = tc
spec.loader.exec_module(tc)

fails = 0
def check(name, cond):
    global fails
    print(f"[{'OK ' if cond else 'XX '}] {name}")
    if not cond:
        fails += 1


def fake_survey():
    sv = tc.Survey(worker_id="tester")
    sv.open_prs = [tc.PRInfo(number=n, head_oid="x", head_ref="b", head_owner="o", head_repo="r",
                             is_draft=False, mergeable="MERGEABLE", author="me", build_success=True,
                             build_failed=False, title=f"PR {n}") for n in (101, 102)]
    sv.n_open_nondraft = 2
    sv.n_reviewable = 2
    sv.reviewable.actionable = [tc.Candidate(101, "x"), tc.Candidate(102, "x")]
    sv.next_auto_stage = "review"
    return sv


def loader():
    return fake_survey(), {}, ["algebra", "topology"]


CFG = SimpleNamespace(logdir=Path("/tmp/tauceti-test"), home=Path(_CFGDIR), state=Path(_CFGDIR) / "state")


async def main():
    cfg = CFG
    app = tc._dashboard_app(cfg, loader=loader)
    async with app.run_test() as pilot:
        for _ in range(20):                      # wait for the background load worker to land
            if app.sv is not None:
                break
            await pilot.pause(0.05)
        check("survey loaded", app.sv is not None)
        check("cursor defaults to next_auto (review)", tc.ALLOWED_TASKS[app.sel] == "review")
        table = app.query_one("#tbl")
        check("table has the 6 kinds", table.row_count == len(tc.ALLOWED_TASKS))

        await pilot.press("down")
        check("down moves to fix-ci", tc.ALLOWED_TASKS[app.sel] == "fix-ci")
        await pilot.press("up")
        await pilot.press("up")
        check("up wraps past review to rebase", tc.ALLOWED_TASKS[app.sel] == "rebase")

        await pilot.press("m")
        check("m cycles model auto->codex", app.model_dial == "codex")
        await pilot.press("s")
        check("s toggles sandbox to host", app.host is True)

        before = table.row_count
        # move the cursor onto review (index 1) and expand: its 2 PRs become sub-rows
        await pilot.press("down")
        await pilot.press("right")
        check("review is expanded", "review" in app.expanded)
        check("expand added PR sub-rows", table.row_count == before + 2)
        await pilot.press("left")
        check("collapse removed them", table.row_count == before)

        # a digit jumps the cursor to that kind and launches a one-round follow-up (then exits)
        await pilot.press("2")
        check("digit 2 selects review", tc.ALLOWED_TASKS[app.sel] == "review")
        await pilot.pause(0.1)
    check("one-round launch set an exec follow-up", app.followup is not None and app.followup[0] == "exec")
    check("follow-up runs review", app.followup is not None and "review" in app.followup[1])

    # persistence: the m/s toggles above should have written a prefs file under XDG_CONFIG_HOME
    prefs_file = tc._prefs_path(CFG)
    check("prefs file written under XDG_CONFIG_HOME", prefs_file.exists() and str(prefs_file).startswith(_CFGDIR))
    saved = json.loads(prefs_file.read_text())
    check("prefs persisted model=codex", saved.get("model") == "codex")
    check("prefs persisted host=True", saved.get("host") is True)

    # restart: a fresh app instance restores the saved dials
    app2 = tc._dashboard_app(CFG, loader=loader)
    async with app2.run_test() as pilot:
        await pilot.pause(0.05)
        check("restart restores model=codex", app2.model_dial == "codex")
        check("restart restores sandbox=host", app2.host is True)


asyncio.run(main())
sys.exit(1 if fails else 0)
