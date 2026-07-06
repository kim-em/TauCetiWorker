#!/usr/bin/env python3
"""macOS: isolate_home must seed $GH_TOKEN from the operator's gh login before repointing $HOME.

gh keeps its token in the login Keychain, which `security` locates via $HOME/Library/Keychains. Once the
worker isolates $HOME to a per-slot dir, that lookup misses and the survey's `gh pr list` fails — the
worker aborts the round (Bryan's report: an auto-assigned worker slot on macOS broke gh's keyring auth;
his workaround was `--worker-id default`). `_seed_gh_token_for_isolation` captures the token while the
real $HOME still reaches the Keychain and exports $GH_TOKEN, which gh honours ahead of the keychain.

This pins the seed's decisions without touching a real Keychain or moving $HOME: it fires only on macOS,
respects an operator-set token, and fails soft (no token, gh error, or a missing gh binary all leave the
env untouched rather than crashing isolation).

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=out, stderr="")


def run_seed(platform, preset, gh_result):
    """Drive _seed_gh_token_for_isolation with a faked platform, starting token env, and `gh auth token`
    result. `preset` names the token env vars to set first ({} = neither set). `gh_result` is a
    CompletedProcess to return, or an Exception to raise. Returns (token_env, called): the GH_TOKEN/
    GITHUB_TOKEN left in os.environ afterwards, and whether `gh auth token` was shelled out to. Saves and
    restores the real os.environ token keys + the patched globals."""
    called = {"v": False}

    def fake_run(argv, **kw):
        called["v"] = True
        check("calls `gh auth token`", argv[:3] == ["gh", "auth", "token"])
        if isinstance(gh_result, Exception):
            raise gh_result
        return gh_result

    saved = {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN")}
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        os.environ.pop(k, None)
    os.environ.update(preset)
    orig_platform, orig_run = tc.agents.sys.platform, tc.agents.subprocess.run
    tc.agents.sys.platform = platform
    tc.agents.subprocess.run = fake_run
    try:
        tc.agents._seed_gh_token_for_isolation()
        token_env = {k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(k) is not None}
    finally:
        tc.agents.sys.platform = orig_platform
        tc.agents.subprocess.run = orig_run
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return token_env, called["v"]


# 1) macOS, no token set, gh returns a token -> $GH_TOKEN exported from it.
env, called = run_seed("darwin", {}, _cp(0, "gho_THETOKEN\n"))
check("darwin: GH_TOKEN seeded from `gh auth token`", env.get("GH_TOKEN") == "gho_THETOKEN")
check("darwin: shelled out to capture it", called)

# 2) macOS, operator already exported GH_TOKEN -> untouched, no shell-out.
env, called = run_seed("darwin", {"GH_TOKEN": "operator"}, _cp(0, "should-not-be-used"))
check("darwin: existing GH_TOKEN respected", env.get("GH_TOKEN") == "operator")
check("darwin: no shell-out when GH_TOKEN already set", not called)

# 3) macOS, GITHUB_TOKEN set (gh honours it too) -> don't seed GH_TOKEN, no shell-out.
env, called = run_seed("darwin", {"GITHUB_TOKEN": "gha"}, _cp(0, "tok"))
check("darwin: GITHUB_TOKEN counts as set -> no GH_TOKEN seeded", "GH_TOKEN" not in env)
check("darwin: no shell-out when GITHUB_TOKEN set", not called)

# 4) macOS, gh not logged in (rc!=0) -> leave env untouched (fail soft).
env, called = run_seed("darwin", {}, _cp(1, ""))
check("darwin: gh failure leaves GH_TOKEN unset", "GH_TOKEN" not in env)
check("darwin: still attempted the capture", called)

# 4b) macOS, rc=0 but empty token -> still unset.
env, _ = run_seed("darwin", {}, _cp(0, "  \n"))
check("darwin: empty token output leaves GH_TOKEN unset", "GH_TOKEN" not in env)

# 5) macOS, gh binary missing / subprocess error -> caught, env untouched, no crash.
env, called = run_seed("darwin", {}, FileNotFoundError("gh"))
check("darwin: missing gh binary doesn't crash, GH_TOKEN unset", "GH_TOKEN" not in env)

# 6) Linux -> no-op: never shells out, never seeds (token isn't $HOME-path-scoped there).
env, called = run_seed("linux", {}, _cp(0, "linuxtok"))
check("linux: no GH_TOKEN seeded", "GH_TOKEN" not in env)
check("linux: no shell-out at all", not called)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
