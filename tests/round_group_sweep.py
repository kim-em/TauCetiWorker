#!/usr/bin/env python3
"""Leaked-background-process sweep (the orphaned build-waiter).

A tool-using `claude` agent backgrounds a long `lake build` and Claude Code's Bash tool waits on it with
a synthesized `until ... do sleep; done` poll-loop (one job-control variant busy-spins a whole core). When
the agent exits 0, that loop has no live parent and reparents to init — surviving forever. The round runs
in its OWN session, and the loop driver's timeout teardown (kill_round_group) only fires on the abnormal
paths, so a round that simply *finishes* never swept its group. reap_round_group closes that gap on every
exit path. This harness reproduces the exact leak (a round that exits 0 leaving a backgrounded grandchild)
and pins that the sweep clears it, plus a direct check that it reaches a whole multi-process group.

Exit 0 = swept as expected; 1 = a straggler survived.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

sys.path.insert(0, str(REPO))
import tauceti_worker as tc

pass_ = 0
fail = 0


def ok(msg: str) -> None:
    global pass_
    print(f"  [PASS] {msg}")
    pass_ += 1


def no(msg: str) -> None:
    global fail
    print(f"  [FAIL] {msg}")
    fail += 1


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


print("== 1. reap_round_group sweeps a whole multi-process session group ==")
# A leader in its own session that spawns a grandchild; both outlive the test window. start_new_session
# makes the leader its own group leader, so its pgid == its pid — exactly spawn_round's guarantee. Use a
# python leader (not `bash -c`, whose non-interactive SIGTERM deferral muddies the timing) so the group
# mirrors a real round: a process that forks a long-lived child and then exits/sleeps.
leader = subprocess.Popen(
    [sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','300']); time.sleep(300)"],
    start_new_session=True,
)
time.sleep(0.5)
pgid = leader.pid
if group_alive(pgid):
    tc.reap_round_group(pgid)
    # reap_round_group kills the leader but, as its parent, we must wait() it so it isn't left a zombie —
    # a zombie still answers kill(pid, 0), which would mask whether the grandchild actually died.
    try:
        leader.wait(2)
    except subprocess.TimeoutExpired:
        pass
    if not group_alive(pgid):
        ok("multi-process group fully swept")
    else:
        no("group survived reap_round_group")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
else:
    no("could not stand up the test group")
    try:
        leader.wait(2)
    except subprocess.TimeoutExpired:
        pass

print("== 2. reap_round_group is a no-op on an already-empty group ==")
try:
    tc.reap_round_group(leader.pid)  # leader and its group are gone now
    ok("no error sweeping an empty group")
except Exception as e:
    no(f"raised on empty group: {e}")

print("== 3. a round that exits 0 leaves a backgrounded grandchild; the sweep clears it ==")
# Drive the real _round through the test HOLD hook: it spawns `sleep <hold>` and returns 0 immediately —
# the leak. Spawn it the way spawn_round does (own session) so its pid is the group id to sweep.
WID = "sweep-test"
env = dict(os.environ, TAUCETI_WORKER_ID=WID, TAUCETI_TEST_HOLD="300")
round_proc = subprocess.Popen(
    [sys.executable, str(REPO / "tauceti"), "_round", "--worker-id", WID],
    start_new_session=True,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
rc = round_proc.wait(60)
group = round_proc.pid  # == pgid (own session)
time.sleep(0.3)
if rc != 0:
    no(f"round exited rc={rc} (expected 0)")
elif not group_alive(group):
    no("round left no grandchild — test can't prove the sweep (HOLD hook may have changed)")
else:
    ok("round exited 0 with a live orphaned grandchild (the leak reproduced)")
    tc.reap_round_group(group)
    time.sleep(0.2)
    if not group_alive(group):
        ok("sweep cleared the leaked grandchild")
    else:
        no("grandchild survived the sweep")
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

print("== 4. run_round_subprocess sweeps the group on a normal (rc 0) return ==")
# The integration point: not just that reap_round_group works, but that run_round_subprocess's `finally`
# actually invokes it on the happy path. Spy on spawn_round to capture the round's pgid, drive a real
# round that leaks a grandchild (TAUCETI_TEST_HOLD), and assert the group is gone once the call returns.
captured = {}
orig_spawn = tc.round.spawn_round  # patch where run_round_subprocess looks it up


def spy_spawn(argv_tail):
    p = orig_spawn(argv_tail)
    captured["pgid"] = p.pid  # == pgid (spawn_round uses start_new_session)
    return p


tc.round.spawn_round = spy_spawn
os.environ["TAUCETI_TEST_HOLD"] = "300"
try:
    rc = tc.run_round_subprocess(["--worker-id", WID])
finally:
    tc.round.spawn_round = orig_spawn
    os.environ.pop("TAUCETI_TEST_HOLD", None)

pgid4 = captured.get("pgid")
if rc != 0:
    no(f"run_round_subprocess returned rc={rc} (expected 0)")
elif pgid4 is None:
    no("spawn_round was never called")
elif group_alive(pgid4):
    no("run_round_subprocess returned with the round group still alive — finally sweep not wired")
    try:
        os.killpg(pgid4, signal.SIGKILL)
    except ProcessLookupError:
        pass
else:
    ok("run_round_subprocess swept the leaked group on normal return")

# Clean the per-worker state this test seeded.
shutil.rmtree(REPO / "state" / WID, ignore_errors=True)

print(f"\n{pass_} passed, {fail} failed")
sys.exit(1 if fail else 0)
