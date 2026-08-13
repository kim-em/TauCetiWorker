#!/usr/bin/env python3
"""The Lean build caches are pooled per machine, not per worker.

Mathlib's cache tool resolves `~/.cache/mathlib` and elan resolves `~/.elan` through $HOME, so the
per-worker $HOME that isolate_home() creates for CREDENTIALS also gave every worker its own copy of
the same public, content-addressed downloads: on a five-worker fleet, half of one week's 10.2 GB of
`.ltar` traffic was a file another worker on the same disk had already fetched, and 22 toolchain
installs covered 6 distinct toolchains. share_build_caches() redirects both back to the login user's
home. These assertions pin the redirect (it must escape the per-worker home and honour an operator's
own value) and, crucially, that the ALREADY-ISOLATED path asserts it too — that path is what a round
child runs, so without it the pooling would only take effect after every loop was restarted.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0

LOGIN_HOME = Path("/home/pretend-operator")
CACHE_VARS = [var for var, _ in tc.agents.SHARED_BUILD_CACHES]


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def clear(env):
    for var in (*CACHE_VARS, "TAUCETI_DATA_HOME"):
        env.pop(var, None)


def main():
    env = tc.agents.os.environ
    orig = {k: env.get(k) for k in (*CACHE_VARS, "TAUCETI_DATA_HOME", "HOME")}
    orig_host_home, orig_platform = tc.agents._host_home, tc.agents.sys.platform
    tc.agents._host_home = lambda: LOGIN_HOME
    try:
        # --- the redirect itself ---
        clear(env)
        resolved = tc.agents.share_build_caches()
        check("MATHLIB_CACHE_DIR is the login user's ~/.cache/mathlib", env["MATHLIB_CACHE_DIR"] == "/home/pretend-operator/.cache/mathlib")
        check("ELAN_HOME is the login user's ~/.elan", env["ELAN_HOME"] == "/home/pretend-operator/.elan")
        check("returns the resolved mapping (for the log line)", resolved == {v: env[v] for v in CACHE_VARS})
        # The whole point: neither may land under the per-worker home, whose whole subtree is what the
        # duplication came from. Checked against every worker home shape, not just this platform's.
        for platform in ("linux", "darwin"):
            tc.agents.sys.platform = platform
            worker_home = tc.agents._worker_iso_home("worker1")
            for var in CACHE_VARS:
                path = Path(env[var])
                check(
                    f"{var} is outside the {platform} per-worker home ({worker_home})",
                    not path.is_relative_to(worker_home),
                )
        tc.agents.sys.platform = orig_platform

        # --- an operator's own value wins (the opt-out for caches on another volume, and for tests) ---
        clear(env)
        env["MATHLIB_CACHE_DIR"] = "/mnt/big/mathlib"
        tc.agents.share_build_caches()
        check("operator MATHLIB_CACHE_DIR preserved", env["MATHLIB_CACHE_DIR"] == "/mnt/big/mathlib")
        check("ELAN_HOME still defaulted alongside it", env["ELAN_HOME"] == "/home/pretend-operator/.elan")

        # --- the already-isolated path (what a round child of an older loop runs) ---
        # isolate_home() returns early on the completion sentinel without touching the filesystem;
        # it must still assert the cache redirects, or the pooling waits for a fleet restart.
        clear(env)
        tc.agents.sys.platform = "linux"
        home = tc.agents._worker_iso_home("worker1")
        env["TAUCETI_DATA_HOME"] = str(home)
        env["HOME"] = str(home)
        returned = tc.agents.isolate_home("worker1")
        check("already-isolated path returns the same home", returned == home)
        for var, parts in tc.agents.SHARED_BUILD_CACHES:
            check(f"already-isolated path sets {var}", env.get(var) == str(LOGIN_HOME.joinpath(*parts)))
            check(f"already-isolated {var} is outside the worker home", not Path(env[var]).is_relative_to(home))
    finally:
        tc.agents._host_home, tc.agents.sys.platform = orig_host_home, orig_platform
        for k, v in orig.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
