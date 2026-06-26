#!/usr/bin/env python3
"""Fork-based PR authoring (kim-em/bubble#320 + TauCetiWorker fork migration).

The worker authors and fixes from the contributor's OWN fork: the branch is pushed there and the PR
is opened from it, so no canonical write access is needed (Bryan's report — a read-only account could
not land roadmap work). This harness pins three pure-ish decisions without touching GitHub or bubble:

  1. `ensure_fork()` resolves the fork BY PARENT (not by name), honors `$TAUCETI_FORK`, creates one
     when absent, and fails closed if a same-named NON-fork squats the name.
  2. `_do_fixlike` skips a tended PR whose head repo was deleted (empty head fields) instead of
     building a `https://github.com//` remote, and otherwise hands bubble the PR's head repo as the
     fork to allow git fetch/push to.
  3. `do_roadmap` points the push at the fork, passes `--allow-push <fork>` to bubble while keeping the
     bubble TARGET canonical, and substitutes the fork owner + worker id into the prompt's `--head`.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

TAUCETI = tc.constants.TAUCETI  # "FormalFrontier/TauCeti"
FORK = "alice/TauCeti"
fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=out, stderr="")


def _repo_list_json(parent_owner, parent_name, name_with_owner=FORK):
    return f'[{{"nameWithOwner":"{name_with_owner}","parent":{{"name":"{parent_name}","owner":{{"login":"{parent_owner}"}}}}}}]'


# ---- 1. ensure_fork() -------------------------------------------------------------------------
def fake_gh(scenario):
    """A `gh_run` stand-in driven by `scenario` (a mutable dict): dispatch on the gh subcommand."""

    def run(argv, **kw):
        if argv[1:3] == ["repo", "list"]:
            return _cp(0, scenario["repo_list"]())
        if argv[1:3] == ["repo", "fork"]:
            scenario["forked"] = True
            return _cp(0, "")
        if argv[1] == "api" and ".fork" in argv:  # the same-name clash probe
            return _cp(0, scenario.get("clash_fork", "true"))
        if argv[1] == "api" and ".permissions.push" in argv:  # can_push probe on the fork
            return _cp(0, scenario.get("can_push", "true"))
        return _cp(0, "")

    return run


def run_ensure_fork(scenario, env_fork=None):
    tc.github.ensure_fork.cache_clear()
    tc.github.gh_run = fake_gh(scenario)
    tc.github.me = lambda: "alice"
    old = os.environ.pop("TAUCETI_FORK", None)
    if env_fork is not None:
        os.environ["TAUCETI_FORK"] = env_fork
    try:
        return tc.github.ensure_fork()
    finally:
        os.environ.pop("TAUCETI_FORK", None)
        if old is not None:
            os.environ["TAUCETI_FORK"] = old


def test_ensure_fork():
    # existing fork resolved by parent (a same-named repo with a DIFFERENT parent must not match)
    sc = {"repo_list": lambda: _repo_list_json("FormalFrontier", "TauCeti")}
    check("ensure_fork: existing fork resolved by parent", run_ensure_fork(sc) == FORK)

    # resolve-by-parent: a same-named repo whose parent is someone ELSE is not our fork
    tc.github.gh_run = fake_gh({"repo_list": lambda: _repo_list_json("SomeoneElse", "TauCeti", "alice/TauCeti")})
    check("ensure_fork: wrong-parent same-name not matched", tc.github._find_fork() is None)

    # no fork yet -> create, then resolve (the list flips to the real fork after `gh repo fork`)
    sc = {
        "forked": False,
        "repo_list": lambda: _repo_list_json("FormalFrontier", "TauCeti") if sc.get("forked") else "[]",
    }
    check("ensure_fork: absent -> create -> resolve", run_ensure_fork(sc) == FORK)

    # a same-named NON-fork squats the name -> Die
    sc = {"forked": False, "repo_list": lambda: "[]", "clash_fork": "false"}
    try:
        run_ensure_fork(sc)
        check("ensure_fork: same-named non-fork -> Die", False)
    except tc.Die:
        check("ensure_fork: same-named non-fork -> Die", True)

    # $TAUCETI_FORK override wins with no repo-list call
    sc = {"repo_list": lambda: (_ for _ in ()).throw(AssertionError("should not list"))}
    check("ensure_fork: $TAUCETI_FORK override", run_ensure_fork(sc, env_fork="bob/MyTauCeti") == "bob/MyTauCeti")

    # fork resolves but the account can't push to it -> Die (explicit false only; None fails open)
    sc = {"repo_list": lambda: _repo_list_json("FormalFrontier", "TauCeti"), "can_push": "false"}
    try:
        run_ensure_fork(sc)
        check("ensure_fork: unpushable fork -> Die", False)
    except tc.Die:
        check("ensure_fork: unpushable fork -> Die", True)


# ---- 2. _do_fixlike: deleted-head guard + fork allow_push -------------------------------------
def test_fixlike():
    pr_dead = types.SimpleNamespace(number=5, head_owner="", head_repo="", head_ref="", head="abc")
    sv = types.SimpleNamespace(open_prs=[pr_dead])
    c = types.SimpleNamespace(pr=5, head="abc")
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude")
    called = []
    tc.work_units.run_in_bubble = lambda *a, **k: called.append(k) or 0
    w = types.SimpleNamespace(claims=types.SimpleNamespace(begin_branch_work=lambda *a: True))
    rc = tc.work_units._do_fixlike(w, sv, c, opts, True, prompt_file="fix.md", label="fix")
    check("fixlike: deleted head -> skip (None, no bubble)", rc is None and not called)

    # valid fork head -> bubble gets allow_push=<head owner/repo>, target the PR
    pr_ok = types.SimpleNamespace(number=7, head_owner="alice", head_repo="TauCeti", head_ref="roadmap/x", head="dead")
    sv = types.SimpleNamespace(open_prs=[pr_ok])
    c = types.SimpleNamespace(pr=7, head="dead")
    cap = {}
    tc.work_units.run_in_bubble = lambda w, target, prompt, opts, **k: cap.update(target=target, **k) or 0
    w = types.SimpleNamespace(
        claims=types.SimpleNamespace(begin_branch_work=lambda *a: True),
        rs=types.SimpleNamespace(bust=lambda *a: None),
    )
    tc.work_units._do_fixlike(w, sv, c, opts, True, prompt_file="fix.md", label="fix")
    check("fixlike: fork head -> allow_push=owner/repo", cap.get("allow_push") == "alice/TauCeti")
    check("fixlike: target is the PR", cap.get("target") == f"{TAUCETI}/pull/7")


# ---- 3. do_roadmap: fork push remote + --allow-push + prompt --head --------------------------
def test_roadmap():
    tmp = Path(tempfile.mkdtemp(prefix="fork-test-"))
    os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"  # avoid an intentions-board network call
    os.environ.pop("TAUCETI_ROADMAP_SKIP", None)
    os.environ.pop("TAUCETI_PUSH_EXPECT", None)
    os.environ["TAUCETI_PUSH_EXPECT"] = "stale"  # must be popped by do_roadmap (create-only on the fork)
    tc.work_units.ensure_fork = lambda: FORK
    tc.work_units.fetch_ref = lambda *a, **k: True
    cap = {}
    tc.work_units.run_in_bubble = lambda w, target, prompt, opts, **k: (
        cap.update(target=target, prompt=prompt, **k) or 0
    )
    w = types.SimpleNamespace(cfg=types.SimpleNamespace(state=tmp, wid="worker3"), gh=None)
    c = types.SimpleNamespace(reason="Topology", pr=0, head="")
    opts = types.SimpleNamespace(agent_name="Claude Code", work_model="claude")
    tc.work_units.do_roadmap(w, None, c, opts, bubble=True)

    check("roadmap: bubble target stays canonical", cap.get("target") == TAUCETI)
    check("roadmap: --allow-push is the fork", cap.get("allow_push") == FORK)
    check("roadmap: push remote is the fork URL", os.environ.get("TAUCETI_PUSH_REMOTE") == f"https://github.com/{FORK}")
    check("roadmap: PUSH_EXPECT popped (create-only)", "TAUCETI_PUSH_EXPECT" not in os.environ)
    prompt = cap.get("prompt", "")
    check("roadmap: prompt has --head <forkowner>:", "--head alice:roadmap/" in prompt)
    check("roadmap: prompt carries the worker id", "worker3" in prompt)
    check("roadmap: no unsubstituted placeholders", "__FORK__" not in prompt and "__WORKERID__" not in prompt)


# ---- 4. ensure_fork_proxy_current: version-gated auth-proxy daemon restart -------------------
def test_proxy_current():
    tmp = Path(tempfile.mkdtemp(prefix="fork-proxy-"))
    stamp = tmp / ".auth-proxy-bubble-version"
    tc.agents._auth_proxy_stamp = lambda: stamp  # host-global stamp (real path uses pwd, not $HOME)
    tc.agents._bubble_version = lambda: "bubble, version 0.7.25"

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _cp(0, "")

    def started():
        return any(a[-3:] == ["gh", "proxy", "start"] for a in calls)

    real_run = tc.agents.subprocess.run
    tc.agents.subprocess.run = fake_run
    try:
        # no stamp yet -> restart the daemon and record the version
        tc.agents.ensure_fork_proxy_current()
        check("proxy: unstamped -> gh proxy start", started())
        check("proxy: stamp written with current version", stamp.read_text().strip() == "bubble, version 0.7.25")

        # stamp matches -> no restart
        calls.clear()
        tc.agents.ensure_fork_proxy_current()
        check("proxy: matching stamp -> no restart", not started())

        # version changed -> restart again and update the stamp
        calls.clear()
        tc.agents._bubble_version = lambda: "bubble, version 0.7.26"
        tc.agents.ensure_fork_proxy_current()
        check("proxy: version change -> restart", started())
        check("proxy: stamp updated", stamp.read_text().strip() == "bubble, version 0.7.26")

        # restart failure is fail-CLOSED: Die, and leave the stamp stale so a later round retries
        stamp.write_text("bubble, version 0.7.25")  # pretend stale again
        tc.agents._bubble_version = lambda: "bubble, version 0.7.27"

        def boom(argv, **kw):
            raise subprocess.CalledProcessError(1, argv)

        tc.agents.subprocess.run = boom
        try:
            tc.agents.ensure_fork_proxy_current()
            check("proxy: restart failure -> Die", False)
        except tc.Die:
            check("proxy: restart failure -> Die", True)
        check("proxy: failed restart leaves stamp stale", stamp.read_text().strip() == "bubble, version 0.7.25")

        # unreadable version -> refresh anyway (fail-closed: currency unverifiable), leave the stamp untouched
        calls.clear()
        tc.agents.subprocess.run = fake_run
        stamp.write_text("bubble, version 0.7.99")
        tc.agents._bubble_version = lambda: ""
        tc.agents.ensure_fork_proxy_current()
        check("proxy: unreadable version -> refresh", started())
        check("proxy: unreadable version -> stamp untouched", stamp.read_text().strip() == "bubble, version 0.7.99")
    finally:
        tc.agents.subprocess.run = real_run


def main():
    test_ensure_fork()
    test_fixlike()
    test_roadmap()
    test_proxy_current()
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
