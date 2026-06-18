#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich", "textual>=8,<9"]
# ///
"""Headless smoke for the Textual dashboard: build the app with a STUB survey loader (no GitHub, no
tty) and drive it with App.run_test(). Covers the lag fix (cursor/dials are local state), expand/
collapse, the launch follow-up, the roadmap-focus picker (exercises the OptionList.OptionSelected
path end-to-end, so attribute-name drift across textual versions is caught), the stale roadmap-row
redraw, prefs persistence + env-override precedence, and the stale-survey load-token guard.
Exit 0 = all checks pass, 1 = a failure."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import time
import sys
from pathlib import Path
from types import SimpleNamespace

# Isolate the prefs file: persistence must land here, not in the operator's real ~/.config.
_CFGDIR = tempfile.mkdtemp(prefix="tauceti-prefs-")
os.environ["XDG_CONFIG_HOME"] = _CFGDIR
os.environ.pop("TAUCETI_ROADMAP_FOCUS", None)   # start from a known (unset) focus state

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


def fake_survey(next_stage="review"):
    sv = tc.Survey(worker_id="tester")
    sv.open_prs = [tc.PRInfo(number=n, head_oid="x", head_ref="b", head_owner="o", head_repo="r",
                             is_draft=False, mergeable="MERGEABLE", author="me", build_success=True,
                             build_failed=False, title=f"PR {n}") for n in (101, 102)]
    sv.n_open_nondraft = 2
    sv.n_reviewable = 2
    sv.reviewable.actionable = [tc.Candidate(101, "x"), tc.Candidate(102, "x")]
    sv.roadmap_focus = tc.roadmap_focus() or "any"
    sv.next_auto_stage = next_stage
    return sv


def loader():
    return fake_survey(), {}, ["algebra", "topology"]


CFG = SimpleNamespace(logdir=Path("/tmp/tauceti-test"), home=Path(_CFGDIR), state=Path(_CFGDIR) / "state")


async def await_survey(app, pilot):
    for _ in range(40):
        if app.sv is not None:
            return
        await pilot.pause(0.05)


async def test_dashboard():
    app = tc._dashboard_app(CFG, loader=loader)
    async with app.run_test() as pilot:
        await await_survey(app, pilot)
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
        await pilot.press("down")        # onto review (index 1)
        await pilot.press("right")
        check("review is expanded", "review" in app.expanded)
        check("expand added PR sub-rows", table.row_count == before + 2)
        await pilot.press("left")
        check("collapse removed them", table.row_count == before)

        # roadmap focus picker: open, move to "topology", select. Drives OptionList end-to-end so a
        # textual rename of the OptionSelected attribute would fail here, not silently at runtime.
        await pilot.press("f")
        await pilot.pause(0.15)
        await pilot.press("down")        # (all areas) -> algebra
        await pilot.press("down")        # algebra -> topology
        await pilot.press("enter")
        await pilot.pause(0.1)
        check("focus picker set env focus to topology", os.environ.get("TAUCETI_ROADMAP_FOCUS") == "topology")
        check("roadmap row focus updated immediately (no stale redraw)", app.sv.roadmap_focus == "topology")

        # a digit jumps the cursor to that kind and launches a one-round follow-up (then exits)
        await pilot.press("2")
        check("digit 2 selects review", tc.ALLOWED_TASKS[app.sel] == "review")
        await pilot.pause(0.1)
    check("one-round launch set an exec follow-up", app.followup is not None and app.followup[0] == "exec")
    check("follow-up runs review", app.followup is not None and "review" in app.followup[1])

    # persistence: the m/s/f changes wrote a prefs file under XDG_CONFIG_HOME
    prefs_file = tc._prefs_path(CFG)
    check("prefs file written under XDG_CONFIG_HOME", prefs_file.exists() and str(prefs_file).startswith(_CFGDIR))
    saved = json.loads(prefs_file.read_text())
    check("prefs persisted model=codex", saved.get("model") == "codex")
    check("prefs persisted host=True", saved.get("host") is True)
    check("prefs persisted user-chosen focus=topology", saved.get("roadmap_focus") == "topology")

    app2 = tc._dashboard_app(CFG, loader=loader)
    async with app2.run_test() as pilot:
        await pilot.pause(0.05)
        check("restart restores model=codex", app2.model_dial == "codex")
        check("restart restores sandbox=host", app2.host is True)


async def test_cursor_before_load():
    """#2: a cursor move made before the first survey lands must not be snapped back by the
    auto-select. Use a slow loader whose next_auto (roadmap, idx 5) differs from where we move."""
    def slow():
        time.sleep(0.6)
        return fake_survey(next_stage="roadmap"), {}, []
    app = tc._dashboard_app(CFG, loader=slow)
    async with app.run_test() as pilot:
        await pilot.pause(0.05)           # load worker is still sleeping
        check("no survey yet (slow loader)", app.sv is None)
        await pilot.press("down")         # rebase(0) -> review(1), and marks _sel_init
        moved = app.sel
        await await_survey(app, pilot)    # first survey now lands
        check("user cursor survives the initial load", app.sel == moved == 1)
        check("auto-select did not snap to next_auto (roadmap)", tc.ALLOWED_TASKS[app.sel] != "roadmap")


async def test_sticky_env_focus():
    """#5: a transient TAUCETI_ROADMAP_FOCUS override must NOT be written into prefs by an unrelated
    dial change, or it becomes sticky on later runs that have no env override."""
    cfgdir = tempfile.mkdtemp(prefix="tauceti-prefs-env-")
    os.environ["XDG_CONFIG_HOME"] = cfgdir
    os.environ["TAUCETI_ROADMAP_FOCUS"] = "EnvOnly"
    try:
        cfg = SimpleNamespace(logdir=Path("/tmp/x"), home=Path(cfgdir), state=Path(cfgdir) / "state")
        app = tc._dashboard_app(cfg, loader=loader)
        async with app.run_test() as pilot:
            await await_survey(app, pilot)
            await pilot.press("m")        # change model only — must not persist the env focus
            await pilot.pause(0.05)
        saved = json.loads(tc._prefs_path(cfg).read_text())
        check("model-only change saved the model", saved.get("model") == "codex")
        check("env-only focus NOT persisted as sticky", saved.get("roadmap_focus") != "EnvOnly")
    finally:
        os.environ.pop("TAUCETI_ROADMAP_FOCUS", None)
        os.environ["XDG_CONFIG_HOME"] = _CFGDIR


def test_load_token():
    """#1: a stale survey result (older load token) must be ignored, not clobber fresher state."""
    app = tc._dashboard_app(CFG, loader=loader)
    app._load_seq = 5
    app.sv = "FRESH"
    app._loaded(3, fake_survey(), {}, [], None)     # seq 3 < 5 -> stale
    check("stale load result ignored", app.sv == "FRESH")


async def run_all():
    await test_dashboard()
    await test_cursor_before_load()
    await test_sticky_env_focus()
    test_load_token()


asyncio.run(run_all())
sys.exit(1 if fails else 0)
