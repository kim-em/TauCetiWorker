#!/usr/bin/env python3
"""Loop pacing bypasses the raw quota cache before every round.

The cache is useful for dashboard/status reads, but a productive round can materially
change usage. Reusing the pre-round reading let loop mode launch round after round until
Claude was exhausted. This pins the integration point without network access.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

calls = []


class FakeQuota:
    def __init__(self, cfg):
        self.cfg = cfg

    def choose(self, forced, *, refresh=False):
        calls.append((forced, refresh))
        return "claude", {}


saved = tc.loop.Quota
tc.loop.Quota = FakeQuota
try:
    tc.loop.choose_model(object(), "claude", None, refresh=True)
    tc.loop.choose_model(object(), "auto", None)
finally:
    tc.loop.Quota = saved

want = [("claude", True), (None, False)]
ok = calls == want
print(f"[{'OK ' if ok else 'XX '}] loop refresh vs ordinary cached read: got={calls!r} want={want!r}")

# Pin the semantic behind the plumbing: a forced read must contact the endpoint even with a valid
# cache, refresh that cache on success, and retain the valid cached verdict across a transient error.
reset_session = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
reset_weekly = (datetime.now(UTC) + timedelta(days=6)).isoformat()
payload = {
    "five_hour": {"utilization": 1, "resets_at": reset_session},
    "seven_day": {"utilization": 1, "resets_at": reset_weekly},
}
q = tc.Quota.__new__(tc.Quota)
q._cached_claude = lambda _fp: payload
q._idle_notes = lambda _readings: ({}, False)
stores = []
q._store_raw = lambda *args: stores.append(args)
http_calls = []
saved_http = tc.quota._http_get_json
tc.quota._http_get_json = lambda *_a, **_k: http_calls.append("fetch") or (200, payload, None)
try:
    q._claude_pass("fp", "token")
    q._claude_pass("fp", "token", refresh=True)
    fetched_on_refresh = http_calls == ["fetch"] and len(stores) == 1

    def offline(*_a, **_k):
        raise tc.GitHubError("temporary network failure")

    tc.quota._http_get_json = offline
    fallback, _ = q._claude_pass("fp", "token", refresh=True)
    cached_fallback = fallback.available
finally:
    tc.quota._http_get_json = saved_http

semantic_ok = fetched_on_refresh and cached_fallback
print(
    f"[{'OK ' if semantic_ok else 'XX '}] forced refresh fetches + stores, transient failure uses valid cache: "
    f"fetches={http_calls!r} stores={len(stores)} fallback={cached_fallback!r}"
)

# Pin the actual driver integration too: both paced and --ignore-quota loops must
# make a fresh availability check before the child is authorized to launch.
driver_calls = []
saved_choose = tc.loop.choose_model
saved_budget = tc.loop.github_budget
saved_round = tc.loop.run_round_subprocess
saved_sleep = tc.loop.time.sleep


def choose(_cfg, agent, _cmd, *, refresh=False):
    driver_calls.append((agent, refresh))
    return "claude", {"claude": tc.Provider("claude", True, "opus")}


def stop(_tail):
    raise KeyboardInterrupt


tc.loop.choose_model = choose
tc.loop.github_budget = lambda: {}
tc.loop.run_round_subprocess = stop
tc.loop.time.sleep = lambda seconds: (_ for _ in ()).throw(AssertionError(f"unexpected sleep({seconds})"))
try:
    for ignore in (False, True):
        args = SimpleNamespace(ignore_quota=ignore, bubble=False, quota_cmd=None, source=None)
        tc.loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["review"], agent="claude")
finally:
    tc.loop.choose_model = saved_choose
    tc.loop.github_budget = saved_budget
    tc.loop.run_round_subprocess = saved_round
    tc.loop.time.sleep = saved_sleep

driver_want = [("claude", True), ("claude", True)]
driver_ok = driver_calls == driver_want
print(
    f"[{'OK ' if driver_ok else 'XX '}] paced and ignore-quota drivers refresh: "
    f"got={driver_calls!r} want={driver_want!r}"
)
sys.exit(0 if ok and semantic_ok and driver_ok else 1)
