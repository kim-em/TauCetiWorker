#!/usr/bin/env python3
"""Cache validity and the bounded post-reset bootstrap — the two live/fail-closed properties of the
Claude pacer, exercised through `Quota.claude()` end to end with a stubbed usage endpoint.

Cache: a usage response may be reused only while it is BOTH fully interpretable AND the wall clock has
not passed a reset it represents. A fixed one-hour TTL happily spans a 5-hour window's reset, and
serving the entry past that reset paces the NEW window against the OLD window's usage. Idle, absent
and malformed payloads are never pinned at all — re-reading them is the only thing that resolves them.

Bootstrap: right after a window rolls, the endpoint reports it with no usage and no reset clock. The
worker won't launch claude while a window reads unreadable, and only launching claude opens the next
window — a fixed-point deadlock that recurred on every reset. It is broken by ONE small `claude -p`
request, recorded in a ledger, followed by cache invalidation and a FRESH usage response. Never a full
round on missing telemetry, never more than one request per reset, and a hard, named block if the
telemetry doesn't come back.

Dependency-free; no network, no real `claude`.
"""

import os
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


os.environ.pop("TAUCETI_PACE", None)  # legacy identity curve
os.environ.pop("CLAUDE_CONFIG_DIR", None)  # creds live at <home>/.claude in this test
tc.quota._claude_keychain_creds = lambda: None  # macOS: never consult the real login Keychain


def iso(delta_s: float) -> str:
    return datetime.fromtimestamp(time.time() + delta_s, tz=UTC).isoformat().replace("+00:00", "Z")


def active_payload(session_pct=10, weekly_pct=10, session_in=2 * 3600, weekly_in=3 * 86400):
    return {
        "five_hour": {"utilization": session_pct, "resets_at": iso(session_in)},
        "seven_day": {"utilization": weekly_pct, "resets_at": iso(weekly_in)},
    }


IDLE = {"five_hour": None, "seven_day": {"utilization": 10, "resets_at": iso(3 * 86400)}}
MALFORMED = {
    "five_hour": {"utilization": 5, "resets_at": "nope"},
    "seven_day": {"utilization": 10, "resets_at": iso(86400)},
}
ABSENT = {"seven_day": {"utilization": 10, "resets_at": iso(86400)}}

TOKEN = "tok-abc"
FP = tc.Quota._fingerprint(TOKEN)


class Endpoint:
    """A scripted usage endpoint. Serves `payloads` in order, repeating the last one, and counts calls
    so a test can prove a cache HIT (no call) or a forced re-fetch (an extra call)."""

    def __init__(self, *payloads, code=200, retry_after=None):
        self.payloads = list(payloads) or [{}]
        self.code = code
        self.retry_after = retry_after
        self.calls = 0

    def __call__(self, url, headers, timeout=15):
        self.calls += 1
        return self.code, self.payloads[min(self.calls - 1, len(self.payloads) - 1)], self.retry_after


_REAL_BOOTSTRAP_REQUEST = tc.quota.Quota._claude_bootstrap_request  # restored for the runner tests below


class Bootstrapper:
    """Stands in for the one small `claude -p` request, counting how many times it was made."""

    def __init__(self, ok=True, detail="ok"):
        self.ok, self.detail, self.calls = ok, detail, 0

    def install(self):
        boot = self

        def _req(_self):
            boot.calls += 1
            return boot.ok, boot.detail

        tc.quota.Quota._claude_bootstrap_request = _req
        return self


def make_quota(*, bootstrap=False, home=None):
    """A Quota over a throwaway home with a Claude credentials file, and its own cache dir."""
    home = home or Path(tempfile.mkdtemp())
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(tc.json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}))
    state = home / "state"
    cfg = tc.Config(
        wid="test",
        home=home,
        state=state,
        checkout=home / "co",
        store_dir=home / "store",
        sbcache=state / "cache" / "scoreboard",
        logdir=home / "logs",
        quota_cache=state / "cache",
    )
    return tc.Quota(cfg, bootstrap=bootstrap)


def reason(prov):
    return prov.error or tc._unavail_reason(prov)[1]


# --- cache: a fully-resolved response is cached, and served without a second call ------------------
ep = Endpoint(active_payload())
tc.quota._http_get_json = ep
q = make_quota()
p1 = q.claude()
p2 = q.claude()
check("resolved payload ⇒ available", (p1.available, p1.model), (True, "opus"))
check("resolved payload is cached ⇒ second read makes no HTTP call", ep.calls, 1)
check("cache hit paces identically", (p2.available, p2.model), (True, "opus"))

entry = tc.json.loads((q.cache_dir / "quota-claude.json").read_text())
check("cache entry carries the account fingerprint", entry["fp"], FP)
check(
    "cache entry expires at the EARLIEST reset it represents (not the TTL)",
    round(entry["valid_until"] - time.time()) in range(2 * 3600 - 2, 2 * 3600 + 1),
    True,
)
check("that expiry is far inside the 1h TTL, which alone would not have caught it", tc.QUOTA_TTL["claude"], 3600)

# --- cache: a complete response must NOT survive its own reset ------------------------------------
# The entry below was fetched seconds ago (well inside the TTL) but its session window rolled 10
# minutes ago. Serving it would pace the fresh window against the spent one's 95%.
q = make_quota()
q.cache_dir.mkdir(parents=True, exist_ok=True)
crossed = {
    "five_hour": {"utilization": 95, "resets_at": iso(-600)},
    "seven_day": {"utilization": 10, "resets_at": iso(86400)},
}
q._store_raw("claude", crossed, FP, time.time() - 600)
check(
    "within TTL",
    time.time() - tc.json.loads((q.cache_dir / "quota-claude.json").read_text())["fetched_at"] < 3600,
    True,
)
check("cached response past its reset ⇒ not served (stored expiry)", q._cached_raw("claude", FP), None)

# A legacy entry written before the expiry existed is re-derived from the payload itself.
(q.cache_dir / "quota-claude.json").write_text(
    tc.json.dumps({"fetched_at": int(time.time()), "fp": FP, "payload": crossed})
)
check("legacy entry with no stored expiry ⇒ still rejected once the reset passed", q._cached_claude(FP), None)
check("...though the raw TTL layer would have served it", q._cached_raw("claude", FP) is not None, True)

# End to end: the crossed entry forces a re-fetch, and the fresh response is what paces.
ep = Endpoint(active_payload(session_pct=1))
tc.quota._http_get_json = ep
p = q.claude()
check("crossing a reset forces a fresh fetch", ep.calls, 1)
check("the fresh response is what paces", (p.available, [w.used for w in p.windows]), (True, [1.0, 10.0]))

# --- cache: unresolved telemetry is never pinned for the TTL ---------------------------------------
for label, payload in (("idle", IDLE), ("malformed", MALFORMED), ("absent", ABSENT)):
    ep = Endpoint(payload)
    tc.quota._http_get_json = ep
    q = make_quota()  # bootstrap off: pure caching behaviour
    q.claude()
    q.claude()
    check(f"{label} payload is not cached (every poll re-reads)", ep.calls, 2)
    check(f"{label} payload leaves no cache entry", q._cached_raw("claude", FP), None)

# --- bootstrap: a read-only pacer diagnoses but never spends ---------------------------------------
boot = Bootstrapper().install()
ep = Endpoint(IDLE)
tc.quota._http_get_json = ep
q = make_quota(bootstrap=False)  # `tauceti status` / the dashboard refresh
p = q.claude()
check("read-only pacer never makes a bootstrap request", boot.calls, 0)
check("read-only pacer still blocks", p.available, False)
check("read-only pacer names the state", reason(p), "session window reset; awaiting initialization")

# --- bootstrap: at most ONE request while the window stays idle ------------------------------------
boot = Bootstrapper().install()
ep = Endpoint(IDLE)
tc.quota._http_get_json = ep
q = make_quota(bootstrap=True)
p = q.claude()
check("an idle window authorizes one bootstrap request", boot.calls, 1)
check("telemetry still idle after it ⇒ still blocked", p.available, False)
check("...and says so", reason(p), "session bootstrap attempted; awaiting fresh usage")
for _ in range(3):
    p = q.claude()
check("repeated idle payloads ⇒ still exactly one bootstrap request", boot.calls, 1)
check("never available on idle telemetry", p.available, False)
check("the state stays explicit", reason(p), "session bootstrap attempted; awaiting fresh usage")
check("a hard block, so --ignore-quota waits it out too", tc._ignore_quota_verdict(None, p), "wait")

# The suppression is bounded, not permanent: once the record ages out, one more attempt is allowed.
led = tc.json.loads((q.cache_dir / tc.CLAUDE_BOOTSTRAP_FILE).read_text())
led["windows"]["session"]["at"] = int(time.time() - tc.CLAUDE_BOOTSTRAP_RETRY_S - 1)
(q.cache_dir / tc.CLAUDE_BOOTSTRAP_FILE).write_text(tc.json.dumps(led))
check("an aged-out record no longer suppresses", q._bootstrap_record(FP, "session"), None)
q.claude()
check("...so a stuck window is retried at a bounded rate", boot.calls, 2)

# The ledger is per account: rotating credentials must not inherit another account's attempt.
check("ledger is keyed by credential fingerprint", q._bootstrap_record("other-account-fp", "session"), None)

# --- bootstrap: success ⇒ cache invalidation + a fresh usage response -------------------------------
boot = Bootstrapper().install()
ep = Endpoint(IDLE, active_payload(session_pct=2))  # idle first, initialized afterwards
tc.quota._http_get_json = ep
q = make_quota(bootstrap=True)
p = q.claude()
check("bootstrap success re-reads usage in the same cycle", ep.calls, 2)
check("exactly one request was made to get there", boot.calls, 1)
check("the fresh, active response decides ⇒ available", (p.available, p.model), (True, "opus"))
check("and it is the fresh numbers that pace", [w.used for w in p.windows], [2.0, 10.0])
check("an initialized window clears its bootstrap record", q._bootstrap_record(FP, "session"), None)
check("the fresh response is now cached", q._cached_raw("claude", FP) is not None, True)
p = q.claude()
check("the next poll is served from cache", (ep.calls, boot.calls, p.available), (2, 1, True))

# A later reset gets its own attempt — the record was cleared, so the fix keeps working every cycle.
ep.payloads.append(IDLE)
ep.calls = len(ep.payloads) - 1  # next call serves the idle payload again
q._forget_raw("claude")
q.claude()
check("a LATER reset gets a fresh bootstrap allowance", boot.calls, 2)

# --- bootstrap: failure ⇒ an informative hard block -------------------------------------------------
boot = Bootstrapper(ok=False, detail="claude exited 1: Rate limit exceeded").install()
ep = Endpoint(IDLE)
tc.quota._http_get_json = ep
q = make_quota(bootstrap=True)
p = q.claude()
check("a failed bootstrap does not make claude available", p.available, False)
check("the failure is reported verbatim", reason(p), "session bootstrap failed: claude exited 1: Rate limit exceeded")
check("a failed bootstrap is still an attempt on record", boot.calls, 1)
p = q.claude()
check("...so it is not retried on the next poll", boot.calls, 1)
check("and the diagnosis persists", reason(p), "session bootstrap failed: claude exited 1: Rate limit exceeded")

# --- bootstrap: only a RECOGNIZED idle window authorizes it ------------------------------------------
for label, payload, want in (
    ("malformed", MALFORMED, "session reset timestamp invalid (five_hour)"),
    ("absent", ABSENT, "session limit missing from usage response"),
):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint(payload)
    p = make_quota(bootstrap=True).claude()
    check(f"{label} telemetry never triggers a bootstrap request", boot.calls, 0)
    check(f"{label} telemetry fails closed", (p.available, reason(p)), (False, want))

# A weekly reset is bootstrapped exactly like a session reset (independent clocks, same machinery).
boot = Bootstrapper().install()
weekly_idle = {"five_hour": {"utilization": 5, "resets_at": iso(3600)}, "seven_day": None}
tc.quota._http_get_json = Endpoint(weekly_idle, active_payload(session_pct=5, weekly_pct=0))
q = make_quota(bootstrap=True)
p = q.claude()
check("a weekly reset bootstraps too", boot.calls, 1)
check("weekly reset resolves to an available provider", p.available, True)

# --- endpoint failures are named, and never bootstrapped ---------------------------------------------
for code, want, ra in (
    (401, "claude usage HTTP 401 (token expired; refresh left to the operator)", None),
    (429, "claude usage HTTP 429 (usage endpoint rate-limited)", 580.0),
    (503, "claude usage HTTP 503", None),
):
    boot = Bootstrapper().install()
    tc.quota._http_get_json = Endpoint({}, code=code, retry_after=ra)
    p = make_quota(bootstrap=True).claude()
    check(f"HTTP {code} names the status code", (p.available, p.error), (False, want))
    check(f"HTTP {code} makes no bootstrap request", boot.calls, 0)
    check(f"HTTP {code} keeps its Retry-After", p.retry_after, ra)

# --- the real bootstrap runner (a stub `claude` on disk) ----------------------------------------------
tc.quota.Quota._claude_bootstrap_request = _REAL_BOOTSTRAP_REQUEST  # no more stubbing: run the real thing
scripts = Path(tempfile.mkdtemp())


def stub_claude(name, body):
    p = scripts / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


q = make_quota(bootstrap=True)
tc.quota.CLAUDE_CMD = stub_claude("ok.sh", "echo ok; exit 0")
check("a successful `claude -p` ⇒ (True, ok)", q._claude_bootstrap_request(), (True, "ok"))

tc.quota.CLAUDE_CMD = stub_claude("fail.sh", "echo 'Invalid API key' >&2; exit 3")
ok, detail = q._claude_bootstrap_request()
check(
    "a failing `claude -p` ⇒ (False, exit code + last line)",
    (ok, detail.endswith("exited 3: Invalid API key")),
    (False, True),
)

tc.quota.CLAUDE_CMD = str(scripts / "does-not-exist")
ok, detail = q._claude_bootstrap_request()
check("a missing claude binary is reported, not raised", (ok, detail.endswith("not found on PATH")), (False, True))

tc.quota.CLAUDE_CMD = stub_claude("hang.sh", "sleep 30")
tc.quota.CLAUDE_BOOTSTRAP_TIMEOUT_S = 1
ok, detail = q._claude_bootstrap_request()
check("a hanging claude is bounded by the timeout", (ok, detail.endswith("timed out after 1s")), (False, True))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
