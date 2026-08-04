"""tauceti_worker.quota — the subscription pacer: read Codex/Claude usage and credentials and decide
which model may run now, plus the isolated-home credential mirroring."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, log
from .constants import CLAUDE_CMD
from .github import GitHubError, _parse_retry_after

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CLAUDE_BETA = "oauth-2025-04-20"

SESSION_WINDOW_S = 5 * 3600

WEEK_WINDOW_S = 7 * 24 * 3600

QUOTA_TTL = {"codex": 600, "claude": 3600}

# Tolerance on a Claude reset clock that reads as already elapsed. Inside it we still treat the window
# as live (it is about to roll); beyond it the endpoint is describing a window that has already ended,
# so its usage figure no longer paces the current one (see _claude_record_state).
CLAUDE_RESET_SKEW_S = 60

# How long ONE recorded bootstrap attempt suppresses further attempts for the same window. A window
# that comes back `active` clears its record immediately, so in the healthy case this never expires;
# it exists only so a permanently idle-reporting endpoint is retried at a bounded rate (once an hour)
# instead of either hammering the provider or wedging forever.
CLAUDE_BOOTSTRAP_RETRY_S = 3600

CLAUDE_BOOTSTRAP_TIMEOUT_S = 120

# How long past its timeout an in-progress reservation is still believed. Covers a request that is
# slower than the timeout we gave it plus clock skew between workers, so a crashed worker's claim
# expires but a live one's is never stolen.
CLAUDE_BOOTSTRAP_STALE_MARGIN_S = 300

# The bootstrap request itself: the smallest useful `claude -p` turn. Its only job is to make ONE real
# request so the provider opens the window its usage endpoint says has reset.
CLAUDE_BOOTSTRAP_PROMPT = "Reply with the single word: ok"

CLAUDE_BOOTSTRAP_FILE = "bootstrap.json"

# Where the shared bootstrap reservation lives, relative to the credential source: every worker
# measuring this account contends for the same file, whatever its worker id or checkout.
CLAUDE_QUOTA_DIRNAME = ".tauceti-quota"

# Environment the bootstrap request must NOT inherit. Every one of these can route a `claude -p` turn
# to different credentials or a different backend than the one whose quota we just measured — an API
# key, a second OAuth token, a proxy, or Bedrock/Vertex/Foundry — which would make the request bill
# something other than the window we are trying to open. CLAUDECODE/CLAUDE_CODE_ENTRYPOINT are dropped
# so the turn does not present as a nested session of whatever launched the worker.
CLAUDE_BOOTSTRAP_DROP_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
    }
)


# --- pacing curve --------------------------------------------------------------------------------
# The pacer decides a window is "under pace" while used% stays under a BUDGET that grows with elapsed
# time. By default that budget is elapsed% itself (the legacy "used% <= elapsed%" identity line), but
# the operator can supply a custom piecewise-linear curve via $TAUCETI_PACE / --pace as "time%:budget%"
# control points, e.g. "0:10,50:70,90:90": allow 10% immediately, ramp to 70% by the halfway mark and
# 90% by 90% of the window, then (unspecified time 100 defaults to budget 100) ramp to the full quota by
# the deadline. Budget is a % of quota and MAY exceed 100 (a value >=100 means "no cap" — used% can't
# exceed 100 anyway). This shapes only the soft PACE; a window at 100% used is still 'exhausted', and
# missing/limit-reached data still fail-closed, both independent of the curve.
_LEGACY_PACE = [(0.0, 0.0), (100.0, 100.0)]  # used% <= elapsed%

_PACE_CACHE: dict[str, list[tuple[float, float]]] = {}


def parse_pace_curve(spec: str | None) -> list[tuple[float, float]]:
    """Parse "t:b,t:b,..." into sorted (time%, budget%) control points, filling the endpoints the
    operator omits: time 0 -> budget 0, time 100 -> budget 100 (full quota by the deadline). An empty /
    None spec is the legacy identity curve (used% <= elapsed%). Raises ValueError on a malformed spec so
    the CLI can reject it up front."""
    pts: dict[float, float] = {}
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok:
            continue
        if ":" not in tok:
            raise ValueError(f"pace point {tok!r} is not in 'time:budget' form (e.g. 50:70)")
        ts, bs = tok.split(":", 1)
        try:
            t, b = float(ts), float(bs)
        except ValueError as e:
            raise ValueError(f"pace point {tok!r} has a non-numeric value") from e
        if not math.isfinite(t) or not math.isfinite(b):  # float() accepts nan/inf/1e999 — reject them
            raise ValueError(f"pace point {tok!r} has a non-finite value")
        if not 0 <= t <= 100:
            raise ValueError(f"pace time {t} is outside 0..100 in {tok!r}")
        if b < 0:
            raise ValueError(f"pace budget {b} must be >= 0 in {tok!r}")
        if t in pts and pts[t] != b:
            raise ValueError(f"pace time {t} given twice with different budgets ({pts[t]} and {b})")
        pts[t] = b
    if not pts:
        # No usable points: a blank/whitespace spec means "unset" (legacy identity); a spec with real
        # structure that parsed to nothing (e.g. "," or ",,") is a typo, not a request for identity —
        # reject it loudly.
        if (spec or "").strip():
            raise ValueError(f"no pace points in {spec!r} (want time:budget, e.g. 0:10,50:70)")
        return list(_LEGACY_PACE)
    pts.setdefault(0.0, 0.0)
    pts.setdefault(100.0, 100.0)
    return sorted(pts.items())


def pace_curve() -> list[tuple[float, float]]:
    """The active pacing curve from $TAUCETI_PACE (read live so --pace / the TUI and loop children all
    see it; parse cached per raw spec). A spec that somehow reaches here malformed — the CLI validates
    --pace up front — falls back to the strict legacy curve rather than silently unlocking spend."""
    raw = os.environ.get("TAUCETI_PACE", "") or ""
    if raw not in _PACE_CACHE:
        try:
            _PACE_CACHE[raw] = parse_pace_curve(raw)
        except ValueError:
            _PACE_CACHE[raw] = list(_LEGACY_PACE)
    return _PACE_CACHE[raw]


def pace_budget(curve: list[tuple[float, float]], elapsed: float) -> float:
    """The max allowed used% at this elapsed%, linearly interpolated over `curve` (sorted, spanning
    time 0..100)."""
    e = max(0.0, min(100.0, elapsed))
    prev_t, prev_b = curve[0]
    for t, b in curve:
        if e <= t:
            return b if t == prev_t else prev_b + (e - prev_t) / (t - prev_t) * (b - prev_b)
        prev_t, prev_b = t, b
    return curve[-1][1]


def pace_zero_plateau(curve: list[tuple[float, float]]) -> float:
    """τ₀ = sup{t : the budget is 0 across the whole of [0, t]} — how long a fresh window stays
    completely unspendable under this curve. 0 means the budget is positive at (or immediately after)
    the start of a window.

        0:0,100:100     → 0    (budget rises the instant the window opens)
        0:10,100:100    → 0    (10% allowed immediately)
        0:0,90:0,100:95 → 90   (nothing may be spent until 90% of the window has elapsed)

    This is what decides whether a just-reset window may be INITIALIZED: a bootstrap request spends,
    and the operator's curve may say that a window this fresh has no budget at all. See
    _idle_init_block — with τ₀ > 0 we cannot even wait it out, because an uninitialized window has no
    reset clock to measure elapsed time against."""
    plateau = 0.0
    for t, b in curve:
        if b != 0:
            break
        plateau = t
    return plateau


def _idle_init_block(curve: list[tuple[float, float]] | None = None) -> str | None:
    """None when the pace curve permits initializing an idle window right now; otherwise the reason it
    does not, phrased for the status line.

    An idle window is interpreted for PACING as the synthetic reading used=0 at elapsed=0. If the budget
    there is positive (or becomes positive immediately after 0) a bootstrap request is within the
    operator's policy. If the curve holds the budget at 0 for a stretch (τ₀ > 0), spending anything on
    this window is against that policy, and TauCeti cannot wait the plateau out either: the window has
    not opened, so there is no clock saying when τ₀% of it will have elapsed. It stays blocked until
    something else initializes the window, the operator changes the curve, or real telemetry appears.
    We do NOT quietly initialize the window to manufacture a clock."""
    tau0 = pace_zero_plateau(pace_curve() if curve is None else curve)
    if tau0 <= 0:
        return None
    return f"pace budget stays 0% through {tau0:g}% of the window"


# --- the raw epistemic state of one quota window -------------------------------------------------
# Read what the endpoint actually said about a window BEFORE any pacing, and keep WHY it is unusable.
# The four states are exhaustive and deliberately not collapsible into each other:
#
#   active     a finite usage% AND a valid, not-yet-elapsed reset clock. The ONLY state that paces.
#   idle       a RECOGNIZED post-reset representation: the window rolled and nothing has opened the
#              next cycle yet (Anthropic reports the window as null, or with an explicit null usage /
#              null reset, in the gap). Never grants availability — it authorizes at most ONE bounded
#              bootstrap request (see _claude_bootstrap_request).
#   absent     the payload carries no record for this window at all (schema drift, a truncated body).
#   malformed  a record exists but is contradictory, non-finite, invalid, or unrecognized.
#
# `absent` and `malformed` FAIL CLOSED: an unreadable hard quota constraint is not the same thing as
# no constraint, so a window we cannot read must never be dropped from the gating set. Only `active`
# reaches the pacing curve.
STATE_ACTIVE = "active"

STATE_IDLE = "idle"

STATE_ABSENT = "absent"

STATE_MALFORMED = "malformed"

# --- pacing statuses ------------------------------------------------------------------------------
# `under-pace` means POSITIVE HEADROOM — strictly under the budget — and nothing else. It is the only
# status that may start a new request. Sitting exactly ON the budget is its own status: a request costs
# something, so used == budget has no room for it, and collapsing the two let a 0% budget authorize
# spending at 0% used (every window at elapsed 0 under the default curve).
STATUS_UNDER_PACE = "under-pace"

STATUS_AT_BUDGET = "at-budget"

STATUS_OVER_PACE = "over-pace"

STATUS_EXHAUSTED = "exhausted"

# Soft = real quota remains and only the burn-pace throttle is holding us; this is what --ignore-quota
# may override. Hard = fail-closed: exhausted, or a window we cannot read / cannot legitimately spend
# against. `unknown` is the generic fail-closed status the codex reader still emits.
SOFT_STATUSES = (STATUS_AT_BUDGET, STATUS_OVER_PACE)

HARD_STATUSES = (STATUS_EXHAUSTED, STATE_IDLE, STATE_ABSENT, STATE_MALFORMED, "unknown")

# How a single JSON field read: omitted, explicitly null, a usable value, or garbage. Collapsing these
# is what let an invalid reset timestamp read the same as an explicit null (⇒ "fresh window").
_F_MISSING, _F_NULL, _F_VALUE, _F_BAD = "missing", "null", "value", "bad"

_ABSENT_REC = object()  # sentinel: this representation has no record for the window at all

_FLAT_KEY = {"session": "five_hour", "weekly": "seven_day"}

_WINDOW_S = {"session": SESSION_WINDOW_S, "weekly": WEEK_WINDOW_S}


@dataclass
class Reading:
    """One Claude quota window as the endpoint reported it, before pacing. `used`/`resets_at` are only
    meaningful when state is `active`; `detail` names the offending condition for the other states and
    `source` says which representation it came from (`limits` or the flat key)."""

    window: str  # session | weekly
    state: str  # active | idle | absent | malformed
    used: float | None = None  # percent 0..100
    resets_at: float | None = None  # epoch seconds
    detail: str | None = None
    source: str | None = None


@dataclass
class Window:
    name: str
    used: float | None  # percent 0..100 (None = unknown)
    elapsed: float | None  # percent 0..100 (None = unknown)
    resets_at: float | None  # epoch seconds
    status: str  # under-pace | over-pace | exhausted | idle | absent | malformed | unknown
    budget: float | None = None  # pace budget (max allowed used%) at this window's elapsed%, if computed
    detail: str | None = None  # why a non-pacing status happened, phrased to follow the window name


@dataclass
class Provider:
    name: str  # codex | claude
    available: bool  # all windows under pace (and a usable model)
    model: str | None  # gpt-5 | opus | None
    windows: list[Window] = field(default_factory=list)
    error: str | None = None
    next_eligible: float | None = None  # epoch when a blocking window should free
    retry_after: float | None = None  # seconds the endpoint asked us to back off (HTTP 429); NOT a
    # quota reset — must not be classified as exhausted/next_eligible
    # The launch-stage bootstrap decision, computed PURELY (see Quota._claude_provider). True only when
    # the sole thing between this provider and a launch is one or more recognized idle windows that the
    # operator's pace curve permits initializing, with every other window active and holding headroom.
    # Nothing acts on it except the explicit launch-stage call; reads never spend.
    bootstrap_eligible: bool = False
    pending_bootstrap: list[str] = field(default_factory=list)  # the idle windows a bootstrap would open


def _finite_num(x: object) -> bool:
    """A usable numeric percentage: a real int/float that isn't a bool and isn't NaN/inf. The usage
    endpoints are reverse-engineered JSON (whose decoder even accepts NaN/Infinity), so anything else is
    treated as missing telemetry — fail-closed — rather than clamped into a spurious value."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _classify_window(
    name: str, used: float | None, elapsed: float | None, resets_at: float | None, limit_reached: bool
) -> Window:
    # Fail CLOSED on missing OR garbage data. These endpoints are reverse-engineered: a schema drift that
    # drops `used`/the reset clock, or hands us a non-number / NaN / inf, must read as 'unknown' (⇒
    # provider unavailable), NEVER as fresh / under-pace, so it can't silently unlock spending. A hard
    # limit_reached still exhausts regardless of the elapsed value. Clamp valid percentages to [0,100].
    e = max(0.0, min(100.0, elapsed)) if _finite_num(elapsed) else None
    if limit_reached:
        return Window(name, used, e, resets_at, STATUS_EXHAUSTED)
    if not _finite_num(used) or e is None:
        return Window(name, used, e, resets_at, "unknown")
    u = max(0.0, min(100.0, used))
    thr = pace_budget(pace_curve(), e)  # max allowed used% at this elapsed%, per the operator's curve
    # Strictly under the budget = headroom for another request. Exactly AT it is not: the next request
    # costs something, so starting one would put us over. (Equality is not an edge case — the default
    # curve's budget is 0 at elapsed 0, so a fresh window sits exactly on its budget.)
    if u >= 100:
        st = STATUS_EXHAUSTED
    elif u < thr:
        st = STATUS_UNDER_PACE
    elif u == thr:
        st = STATUS_AT_BUDGET
    else:
        st = STATUS_OVER_PACE
    return Window(name, used, e, resets_at, st, thr)


def _parse_iso(s: object) -> float | None:
    """An ISO-8601 timestamp as epoch seconds, or None when it is not one. Callers must NOT read None
    as "no timestamp" — see _reset_kind, which keeps 'absent', 'explicitly null' and 'unparseable'
    apart, because a null reset clock is a reset window and garbage is telemetry we cannot read."""
    if not isinstance(s, str) or not s:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


# --- Claude usage records: parse ONE window, from ONE representation ------------------------------
def _field(rec: dict, *names: str) -> tuple[bool, object]:
    """(present, raw) for the first of `names` the record actually carries. An OMITTED field and one
    explicitly set to null are different statements and must stay distinguishable."""
    for n in names:
        if n in rec:
            return True, rec[n]
    return False, None


def _usage_kind(rec: dict) -> tuple[str, float | None]:
    """Classify a record's usage percentage: missing / explicitly null / a usable value / garbage.
    Non-numeric, boolean, NaN/inf and negative values are `bad` — never silently a zero, which is the
    single most permissive reading there is."""
    present, raw = _field(rec, "percent", "utilization")
    if not present:
        return _F_MISSING, None
    if raw is None:
        return _F_NULL, None
    if not _finite_num(raw) or raw < 0:
        return _F_BAD, None
    return _F_VALUE, float(raw)


def _reset_kind(rec: dict) -> tuple[str, float | None]:
    """Classify a record's reset clock: missing / explicitly null / a parsed epoch / invalid. An
    unparseable or wrongly-typed timestamp must not collapse into the same value as an explicit null:
    null is the endpoint saying "no window open" (idle), garbage is a clock we cannot read (malformed
    ⇒ fail closed). Both used to parse to None and read as a fresh window."""
    present, raw = _field(rec, "resets_at")
    if not present:
        return _F_MISSING, None
    if raw is None:
        return _F_NULL, None
    ts = _parse_iso(raw)
    return (_F_VALUE, ts) if ts is not None else (_F_BAD, None)


def _claude_record_state(rec: dict, now: float, window_s: float) -> tuple[str, str | None, float | None, float | None]:
    """(state, detail, used, resets_at) for one PRESENT window record, over the two 4-valued fields:

        usage \\ reset | value              | null    | missing   | invalid
        --------------+--------------------+---------+-----------+----------
        value         | active (or idle if | 0: idle | 0: idle   | malformed
                      | the clock elapsed) | >0: mal | >0: mal   |
        null          | malformed          | idle    | idle      | malformed
        missing       | malformed          | idle    | malformed | malformed
        invalid       | malformed          | malform | malformed | malformed

    The shape of the table is one rule: `idle` needs an EXPLICIT statement that the window is not open
    (a null field, or a zero usage with no clock) and nothing contradicting it. A record that simply
    says nothing at all (both fields omitted) is unrecognized, not idle — schema drift must not be able
    to delete a hard quota constraint by dropping fields. A usage figure with no clock (>0%) and a live
    clock with no usage figure are both contradictions: real spend we cannot pace against."""
    uk, used = _usage_kind(rec)
    rk, resets = _reset_kind(rec)
    if uk == _F_BAD:
        return STATE_MALFORMED, "usage figure is not a usable percentage", None, resets
    if rk == _F_BAD:
        return STATE_MALFORMED, "reset timestamp invalid", used, None
    if rk == _F_VALUE:
        if uk != _F_VALUE:
            why = "usage figure missing" if uk == _F_MISSING else "usage figure reported as null"
            return STATE_MALFORMED, f"{why} with a live reset clock", None, resets
        if resets < now - CLAUDE_RESET_SKEW_S:
            # The clock itself says this window already ended; its usage% describes the PREVIOUS cycle,
            # so pacing on it would be wrong in both directions. Treat it as the post-reset gap.
            return STATE_IDLE, "reset clock already elapsed", used, resets
        if resets - now > window_s + CLAUDE_RESET_SKEW_S:
            # A clock further out than the window is long cannot describe this window. Parsing is not
            # validation: a sentinel far-future timestamp would otherwise clamp elapsed to 0 and pace as
            # if the window had just opened, on a number we know is wrong. Fail closed instead.
            return STATE_MALFORMED, f"reset timestamp implausible ({_hours(resets - now)} away)", None, None
        return STATE_ACTIVE, None, used, resets
    # No reset clock stated at all (explicitly null, or omitted).
    if uk == _F_VALUE:
        if used > 0:
            return STATE_MALFORMED, f"usage {used:g}% reported with no reset clock", used, None
        return STATE_IDLE, None, used, None
    if _F_NULL in (uk, rk):
        return STATE_IDLE, None, None, None
    return STATE_MALFORMED, "record carries neither a usage figure nor a reset clock", None, None


def _hours(seconds: float) -> str:
    """A reset distance, phrased for an operator: hours under a day, else days."""
    return f"{seconds / 3600:.0f}h" if abs(seconds) < 48 * 3600 else f"{seconds / 86400:.0f}d"


def _claude_reading(window: str, source: str, rec: object, now: float) -> Reading:
    """One window, read out of ONE representation. The record itself has three shapes before its
    fields matter: missing entirely (absent — fail closed), an explicit JSON null (a positive "this
    window is not open" ⇒ idle), or a value that had better be an object."""
    if rec is _ABSENT_REC:
        return Reading(window, STATE_ABSENT, detail="limit missing from usage response", source=source)
    if rec is None:
        return Reading(window, STATE_IDLE, source=source)
    if not isinstance(rec, dict):
        return Reading(window, STATE_MALFORMED, detail=f"record is not an object ({source})", source=source)
    state, detail, used, resets = _claude_record_state(rec, now, _WINDOW_S[window])
    if detail and state == STATE_MALFORMED:
        detail = f"{detail} ({source})"
    return Reading(window, state, used, resets, detail, source)


def _claude_limits_record(payload: dict, window: str) -> object:
    """This window's entry in the structured `limits` array, or _ABSENT_REC. Each entry carries a
    `group`/`kind` (session | weekly_all | weekly_scoped), a `percent`, a `resets_at` and a `scope`
    that is non-null for the per-model weekly caps (which don't gate the worker's opus, so they are
    skipped). Looked up PER WINDOW: an array that carries only the weekly must not cost us the weekly
    just because the session entry is missing."""
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return _ABSENT_REC
    for lim in limits:
        if not isinstance(lim, dict):
            continue
        group = str(lim.get("group") or lim.get("kind") or "")
        if window == "session" and group == "session":
            return lim
        if window == "weekly" and group.startswith("weekly") and not lim.get("scope"):
            return lim  # the unscoped overall weekly (weekly_all); model-scoped caps are skipped
    return _ABSENT_REC


def _claude_flat_record(payload: dict, window: str) -> object:
    """This window's legacy flat record (`five_hour` / `seven_day`), or _ABSENT_REC when the key is not
    in the payload at all. A key present with a null value is NOT absent — it is a record."""
    key = _FLAT_KEY[window]
    return payload[key] if key in payload else _ABSENT_REC


def _claude_window_reading(payload: dict, window: str, now: float | None = None) -> Reading:
    """The reading for ONE window, judged independently of the other.

    SOURCE PRECEDENCE — the structured `limits` array is AUTHORITATIVE per window: if it carries a
    record for this window, that record decides the state, including idle and malformed, and the legacy
    flat key is not consulted. Only when `limits` has no record for this window (or is absent / not a
    list) do we fall back to the flat key.

    Letting a healthy-looking flat record outvote a present-but-broken `limits` record would be exactly
    the failure this whole model exists to prevent: the newer representation is what the provider is
    actually reporting, and "the legacy field still looks fine" is not evidence that a malformed current
    record is safe to ignore. The two windows resolve their source separately: the session's record
    being absent from `limits` says nothing about the weekly's."""
    now = time.time() if now is None else now
    rec = _claude_limits_record(payload, window)
    if rec is not _ABSENT_REC:
        return _claude_reading(window, "limits", rec, now)
    return _claude_reading(window, _FLAT_KEY[window], _claude_flat_record(payload, window), now)


def _claude_payload_problem(payload: object) -> str | None:
    """None when this is a usage response we can even begin to read — a JSON object — else why not.
    A truthy list/string/number is not an object: reading it would raise, and an exception in the pacer
    is not a quota verdict. Applied to fresh responses AND to anything we load from cache."""
    if isinstance(payload, dict):
        return None
    return f"claude usage response is not a JSON object (got {type(payload).__name__})"


def _claude_readings(payload: dict, now: float | None = None) -> list[Reading]:
    """The session and the overall (unscoped) weekly, each read on its own. These two gate the
    worker's opus; the per-model weekly caps do not."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict):  # never .get() on a non-object; callers treat both as hard blocks
        payload = {}
    return [_claude_window_reading(payload, w, now) for w in ("session", "weekly")]


def _claude_valid_until(readings: list[Reading]) -> float | None:
    """The instant a payload stops describing reality — the EARLIEST reset clock among its windows —
    or None when the payload is not fully resolved (any window not `active`).

    This is the whole cache rule: a response may be reused only while it is both fully and safely
    interpretable AND the wall clock has not yet passed a reset it represents. A fixed TTL alone let a
    complete response survive across its own 5-hour reset, after which the worker paced the NEW window
    against the OLD window's usage; and an idle/absent/malformed payload must never be pinned at all,
    because re-reading it is the only thing that can resolve it."""
    if any(r.state != STATE_ACTIVE or r.resets_at is None for r in readings):
        return None
    return min(r.resets_at for r in readings)


def _idle_phrase(r: Reading) -> str:
    """The default account of an idle window: it reset, and nothing has opened the next cycle yet —
    plus, when the operator's pace curve is what forbids initializing it, that reason instead of the
    raw telemetry note. An operator seeing this needs to know whether TauCeti is waiting on the
    provider or on their own curve."""
    blocked = _idle_init_block()
    why = blocked or r.detail
    return "window reset; awaiting initialization" + (f" ({why})" if why else "")


def _window_from_reading(r: Reading, note: str | None = None) -> Window:
    """Apply pacing to an `active` reading; carry every other state through verbatim so the caller can
    say what actually happened instead of a generic "usage unknown". Elapsed is derived from the fixed
    window length, since the endpoint gives a reset clock rather than an elapsed fraction."""
    if r.state == STATE_ACTIVE:
        window_s = _WINDOW_S[r.window]
        elapsed = (window_s - (r.resets_at - time.time())) / window_s * 100
        return _classify_window(r.window, r.used, elapsed, r.resets_at, False)
    detail = note or (_idle_phrase(r) if r.state == STATE_IDLE else r.detail)
    # An idle window is interpreted for PACING as the synthetic reading used=0 at elapsed=0 — recorded
    # on the Window so the status output and the bootstrap policy agree about what budget applies. The
    # STATE stays `idle`: this is a policy interpretation, not telemetry we were given.
    budget = pace_budget(pace_curve(), 0.0) if r.state == STATE_IDLE else None
    return Window(r.window, r.used, 0.0 if r.state == STATE_IDLE else None, r.resets_at, r.state, budget, detail)


def _tail_detail(out: str | None, limit: int = 120) -> str:
    """The last non-empty line of a subprocess's output, trimmed to one short single-line clause — the
    status line is one line, and an agent CLI's failure output is not."""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return ": " + (tail if len(tail) <= limit else tail[: limit - 1] + "…")


def _http_get_json(url: str, headers: dict, timeout: int = 15) -> tuple[int, dict, float | None]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return e.code, {}, _parse_retry_after(e.headers.get("Retry-After"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        raise GitHubError(f"usage fetch failed: {e}") from e


def _read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _safe_exists(path: Path) -> bool:
    """Path.exists() that never raises. A permission-denied probe (EPERM/EACCES — e.g. a sandbox or
    macOS data protection that walls off ~/.codex or ~/.claude) degrades to False instead of crashing,
    matching how _read_json_file swallows OSError. Use it to probe credential/config paths under the
    operator's home, whose permissions we don't control."""
    try:
        return path.exists()
    except OSError:
        return False


def _claude_keychain_attempts() -> list[list[str]]:
    """The `security` reads that locate Claude Code's Keychain item: service "Claude Code-credentials"
    keyed by the login user, then a service-only fallback for older CLI builds that stored it without an
    account (https://github.com/anthropics/claude-code/issues/9403)."""
    service = "Claude Code-credentials"
    user = os.environ.get("USER") or ""
    attempts = []
    if user:
        attempts.append(["security", "find-generic-password", "-s", service, "-a", user, "-w"])
    attempts.append(["security", "find-generic-password", "-s", service, "-w"])
    return attempts


def _claude_keychain_creds() -> dict | None:
    """macOS only: read Claude Code's OAuth blob from the login Keychain, where the CLI keeps creds
    instead of <config>/.credentials.json. Returns the same {"claudeAiOauth": {…}} dict as the file,
    or None when absent / locked / malformed.

    READ-ONLY on purpose — never writes the Keychain. The Keychain is one per-login-user store shared
    with the operator's interactive claude, and its single-use OAuth refresh token rotates on refresh.
    If the pacer refreshed and wrote it back, it would rotate the token out from under the operator's
    claude and log it out. So on token expiry the pacer just reports Claude unavailable for the cycle;
    the spawned claude refreshes the Keychain on its own runs.

    The item's service name is "Claude Code-credentials" with the login user as the account; older CLI
    builds stored it without an account, so fall back to a service-only search
    (https://github.com/anthropics/claude-code/issues/9403)."""
    for cmd in _claude_keychain_attempts():
        try:
            # Bound the read: an unattended GUI ACL prompt would otherwise block the pacer indefinitely
            # (headless/SSH returns 36 right away instead). On timeout, treat Claude as unavailable.
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except OSError:
            return None
        except subprocess.TimeoutExpired:
            log(
                "claude creds: reading the login Keychain timed out (a Keychain access prompt may be "
                "waiting unanswered); grant access once, or run `security unlock-keychain`. Treating "
                "Claude as unavailable."
            )
            return None
        if p.returncode == 0:
            try:
                return json.loads(p.stdout.strip())
            except json.JSONDecodeError:
                return None
        if p.returncode == 36:  # errSecInteractionNotAllowed — Keychain locked (typical under SSH/headless)
            log(
                "claude creds: the login Keychain is locked (errSecInteractionNotAllowed); run "
                "`security unlock-keychain` to let the pacer read them. Treating Claude as unavailable."
            )
            return None
        # else (e.g. 44 = errSecItemNotFound for this service/account) → try the next attempt
    return None


def _claude_keychain_creds_interactive() -> dict | None:
    """macOS only: read the Claude OAuth blob from the login Keychain, INTERACTIVELY. Unlike the pacer's
    read this allows the GUI access prompt (long timeout, not 15s) and, if the Keychain is locked
    (errSecInteractionNotAllowed), runs `security unlock-keychain` once (it prompts for the login
    password on the terminal) and retries. Used to seed the bubble, where the in-container claude can't
    reach the host Keychain and needs a .credentials.json to be staged for it. Returns the
    {"claudeAiOauth": {…}} dict or None."""
    for unlocked in (False, True):
        locked = False
        for cmd in _claude_keychain_attempts():
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if p.returncode == 0:
                try:
                    return json.loads(p.stdout.strip())
                except json.JSONDecodeError:
                    return None
            if p.returncode == 36:  # errSecInteractionNotAllowed (Keychain locked)
                locked = True
        if locked and not unlocked:
            log("claude creds: the login Keychain is locked; unlocking it (enter your login password if prompted)…")
            try:
                subprocess.run(["security", "unlock-keychain"], timeout=120)  # interactive (inherits tty)
            except (OSError, subprocess.TimeoutExpired):
                return None
            continue
        return None
    return None


def codex_dir(home: Path) -> Path:
    """Where Codex keeps its config + credentials. $CODEX_HOME wins (the same var codex itself honors);
    else the conventional <home>/.codex. Isolation repoints $CODEX_HOME at the per-worker copy, and on
    macOS that redirect IS the isolation: $HOME stays at the operator's so the login Keychain remains
    reachable, so <home>/.codex would otherwise resolve to the operator's own credential."""
    d = os.environ.get("CODEX_HOME")
    return Path(d) if d else home / ".codex"


def claude_dir(home: Path) -> Path:
    """Where Claude Code keeps its config + credentials. $CLAUDE_CONFIG_DIR wins (the same var Claude
    Code itself honors for a non-default config location, e.g. switching personal/work accounts); else
    the conventional <home>/.claude. Isolation repoints $CLAUDE_CONFIG_DIR at the per-worker copy."""
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) if d else home / ".claude"


def _write_json_atomic(path: Path, data: dict) -> None:
    """Non-corrupting credential-file write: a UNIQUE temp file in the same dir (a concurrent writer
    can't consume ours), preserve the existing mode (else 0600), fsync, then atomic rename. Raises on
    failure so the caller treats it as 'unavailable' rather than crashing. (Cross-process serialization
    vs the official CLIs is handled by --isolate-home giving each worker its own credential copy.)"""
    import tempfile

    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, tmpname = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmpname, mode)
        os.replace(tmpname, path)
    except OSError:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def _read_marker(p: Path) -> str | None:
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


# Written in place of the operator's real codex refresh token in mirrored worker auth.json. codex-cli
# >=0.139 won't parse an auth.json missing `refresh_token`; with a valid access token it never uses it,
# so this constant satisfies the parser while the worker still holds no token that could rotate the
# operator's single-use credential. See _mirror_creds_file.
CODEX_RT_PLACEHOLDER = "rt.0.tauceti-worker-placeholder-never-a-real-refresh-token"


def _mirror_creds_file(
    src: Path,
    dst: Path,
    *,
    block_key: str,
    tok_key: str,
    rt_key: str,
    rt_placeholder: str | None = None,
) -> None:
    """Copy src -> dst WITHOUT the operator's real refresh token, whenever src's access token differs from
    the copy we hold (or that copy still carries a real refresh token to strip). src is the operator's
    single-writer live credential file and is authoritative — including across an account switch, where the
    operator rotates to a *different* account whose token carries an unrelated (often earlier) expiry. We do
    NOT compare expiry: a torn read of src surfaces as invalid JSON, which `_read_json_file` turns into None
    and we skip on below (dst left untouched), so a partial read can never present as a valid-but-stale
    credential for an expiry check to catch. Within one account the operator only refreshes forward, so the
    only thing an expiry comparison ever actually rejected was a legitimate account switch to a token with an
    earlier expiry — wedging the worker on the prior account until the two accounts' expiries happened to
    cross. A missing/unreadable src leaves dst untouched — never blank a good copy. The real refresh token is
    never copied through, so nothing in the worker (pacer or spawned agent) can rotate the operator's
    single-use token and invalidate their copy.

    rt_placeholder: when None, the refresh-token field is omitted entirely (Claude). When set, the field is
    written with this constant value instead — codex-cli >=0.139 refuses to parse an auth.json that lacks
    `refresh_token`, but with a valid (frequently re-mirrored) access token it never reads or rotates it,
    so a placeholder satisfies the parser while keeping the guarantee that the worker holds no real refresh
    token. (Verified: codex runs read-only `exec` with a bogus refresh_token and leaves it untouched.)"""
    sd = _read_json_file(src)
    if not sd:
        return  # source unreadable this cycle — keep what we have
    sblk = sd.get(block_key) or {}
    stok = sblk.get(tok_key)
    if not stok:
        return  # no usable source access token — nothing to mirror
    dd = _read_json_file(dst) or {}
    dblk = dd.get(block_key) or {}
    if dblk.get(tok_key) == stok and dblk.get(rt_key) == rt_placeholder:
        return  # access token current AND refresh field already normalized
    # else: a changed access token (a same-account refresh OR a switch to a different account), or a real
    # refresh token still present (e.g. the once-only isolate_home seed copied the full creds) — re-write,
    # replacing the refresh token with the placeholder (or omitting). src is authoritative; we do not second-
    # guess it by comparing expiry (see the docstring — that only ever wedged the worker across switches).
    out = dict(sd)
    blk = dict(sblk)
    if rt_placeholder is None:
        blk.pop(rt_key, None)
    else:
        blk[rt_key] = rt_placeholder
    out[block_key] = blk
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_json_atomic(dst, out)
    except OSError:
        pass  # best-effort; a failed mirror just keeps the prior copy


def mirror_creds(cfg: Config) -> None:
    """Keep an isolated worker's credential copies in step with the operator's real, externally-refreshed
    files — without ever using a refresh token. The operator runs their own processes that rotate
    ~/.claude/.credentials.json and ~/.codex/auth.json (they own the single-use refresh token); the worker
    only READS those and mirrors any changed access token into its isolated home, refresh token stripped
    (including across an operator account switch, whose new token may carry an earlier expiry).
    No-op when not isolated (no seed marker ⇒ the worker reads the live file directly) or on macOS (the
    Keychain is the store; the keychain-first pacer and _stage_claude_creds_for_bubble handle it). Safe to
    call every pacer cycle and before every bubble launch: in steady state it is two small reads + a string
    compare and no write."""
    if sys.platform == "darwin":
        return
    iso_claude = claude_dir(cfg.home)
    src_claude = _read_marker(iso_claude / ".tauceti-creds-source")
    if src_claude:
        _mirror_creds_file(
            Path(src_claude) / ".credentials.json",
            iso_claude / ".credentials.json",
            block_key="claudeAiOauth",
            tok_key="accessToken",
            rt_key="refreshToken",
        )
    src_codex = _read_marker(codex_dir(cfg.home) / ".tauceti-creds-source")
    if src_codex:  # absent on homes seeded before this marker existed
        _mirror_creds_file(
            Path(src_codex) / "auth.json",
            codex_dir(cfg.home) / "auth.json",
            block_key="tokens",
            tok_key="access_token",
            rt_key="refresh_token",
            rt_placeholder=CODEX_RT_PLACEHOLDER,
        )


def _jwt_claims(token: str | None) -> dict:
    """Best-effort decode of a JWT's payload. These tokens are read for IDENTITY ONLY — never to make
    an authorization decision — so an unparseable one degrades to "unknown account" rather than raising:
    the signature is not checked, and a forged token would only mislabel a credential the forger already
    holds. Returns {} for anything that is not a decodable JWT payload object."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _as_dict(value) -> dict:
    """A dict, or {} for anything else. Every level of a credential file is attacker-adjacent in the
    weak sense that we do not control who wrote it — a partially-written file, a hand-edited one, or a
    future codex schema change all arrive here as valid JSON of the wrong SHAPE. Reading it must degrade
    to "unknown account" (which fails closed) rather than raising an AttributeError out of a doctor row
    or a preflight check."""
    return value if isinstance(value, dict) else {}


def _str_or_none(value) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class _TokenIdentity:
    """The identity ONE token asserts. Kept per-token, and never merged field-by-field across tokens:
    an account id from one token beside an email from another describes an account that may not exist.
    Correlation is the whole point — see CodexAccount.of()."""

    account_id: str | None
    email: str | None
    plan: str | None

    @classmethod
    def of(cls, claims: dict) -> _TokenIdentity:
        auth = _as_dict(claims.get("https://api.openai.com/auth"))
        profile = _as_dict(claims.get("https://api.openai.com/profile"))
        return cls(
            account_id=_str_or_none(auth.get("chatgpt_account_id")) or _str_or_none(claims.get("chatgpt_account_id")),
            # codex's own decoder accepts the email from either the namespaced profile claim or the top
            # level, so we read both rather than assuming one shape.
            email=_str_or_none(profile.get("email")) or _str_or_none(claims.get("email")),
            plan=_str_or_none(auth.get("chatgpt_plan_type")) or _str_or_none(claims.get("chatgpt_plan_type")),
        )

    @property
    def empty(self) -> bool:
        return not (self.account_id or self.email or self.plan)


def _para(text: str) -> str:
    """Wrap one paragraph for a terminal. These messages interpolate email addresses of wildly varying
    length, so hand-wrapped source lines go ragged for the operators who most need to read them.

    Never split a long word: the long words here are paths and email addresses, and both must survive
    intact to be copied, pasted, or compared. Default textwrap would break them at their hyphens
    (`kevin-other@…`, `/home/kim/.codex-tauceti`), so an over-width line is the lesser evil."""
    return textwrap.fill(" ".join(text.split()), width=88, break_long_words=False, break_on_hyphens=False)


@dataclass(frozen=True)
class CodexAccount:
    """Which ChatGPT account a Codex credential will spend under.

    `conflict` means the credential's own identity claims disagree with each other, which is what a
    half-written auth.json looks like (an external swapper interrupted between fields, say). We report
    it rather than picking a winner: a mismatch we describe wrongly is worse than one we decline to
    resolve, since the whole point of --account is to be believed."""

    auth_mode: str | None
    account_id: str | None
    email: str | None
    plan: str | None
    conflict: bool = False

    @property
    def describes_an_account(self) -> bool:
        """False for an API-key credential, which carries no ChatGPT account identity at all."""
        return bool(self.account_id or self.email)

    def matches(self, requested: str) -> bool:
        """--account accepts either the email or the workspace UUID; both are what the operator can
        actually see (the email in ChatGPT's UI, the UUID in `tauceti doctor`)."""
        want = requested.strip().lower()
        return bool(want) and want in {v.lower() for v in (self.email, self.account_id) if v}

    def describe(self) -> str:
        who = self.email or self.account_id or f"an unidentified {self.auth_mode or 'unknown'} credential"
        return f"{who} (plan: {self.plan})" if self.plan else who


class Quota:
    """The pacer. Every read here is pure: it may fetch usage and cache it, but it never spends quota.
    Breaking a post-reset deadlock costs a real request, so it lives behind one explicit method —
    authorize_claude_launch — that a caller invokes only once it has actual work to run. A dashboard
    refresh, `tauceti status`, or an `auto` selection that lands on codex must never spend."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache_dir = cfg.quota_cache

    @staticmethod
    def _fingerprint(*parts: str | None) -> str | None:
        """A short, stable id for the credentials a cache entry was fetched under. Returns None when there
        is nothing to fingerprint (no creds), which never matches a stored fp, so the cache reads as a miss."""
        raw = "|".join(p for p in parts if p)
        return hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else None

    def _cached_raw(self, provider: str, fp: str | None) -> dict | None:
        p = self.cache_dir / f"quota-{provider}.json"
        d = _read_json_file(p)
        if not d:
            return None
        # A cache entry is only valid for the SAME account it was fetched under. When the operator rotates
        # to a different account (or an external refresh swaps the token), fp changes ⇒ the stale entry is
        # for the wrong account and must be re-fetched immediately, not served until the TTL lapses.
        if d.get("fp") != fp:
            return None
        if time.time() - d.get("fetched_at", 0) > QUOTA_TTL[provider]:
            return None
        # A payload may also carry its own expiry: the point at which it stops describing the windows
        # it was fetched for (the earliest reset clock in it). The TTL is a staleness bound, NOT a
        # correctness one — a one-hour claude TTL happily spans a 5-hour window's reset, and serving
        # the entry past that reset paces the new window against the old one's usage.
        vu = d.get("valid_until")
        if isinstance(vu, (int, float)) and time.time() >= vu:
            return None
        return d.get("payload")

    def _store_raw(self, provider: str, payload: dict, fp: str | None, valid_until: float | None = None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {"fetched_at": int(time.time()), "fp": fp, "payload": payload}
        if valid_until is not None:
            entry["valid_until"] = valid_until
        (self.cache_dir / f"quota-{provider}.json").write_text(json.dumps(entry))

    def _forget_raw(self, provider: str) -> None:
        """Drop a provider's cached payload so the next read must go to the network. Used after a
        bootstrap request: whatever we hold predates the request that was meant to change it."""
        try:
            (self.cache_dir / f"quota-{provider}.json").unlink()
        except OSError:
            pass

    # --- Codex -------------------------------------------------------------
    def _codex_creds(self) -> dict | None:
        return _read_json_file(codex_dir(self.cfg.home) / "auth.json")

    def _codex_account_id(self, auth: dict) -> str | None:
        tok = _as_dict(auth.get("tokens"))
        if _str_or_none(tok.get("account_id")):
            return tok["account_id"]
        claims = _jwt_claims(tok.get("id_token"))
        return _str_or_none(claims.get("chatgpt_account_id")) or _str_or_none(claims.get("account_id"))

    def codex_account(self) -> CodexAccount | None:
        """Identity of the Codex account this worker's credential spends under, or None with no
        credential file at all.

        Read from $CODEX_HOME/auth.json, because that is the file `codex` itself reads — `codex login
        status` prints only "Logged in using ChatGPT" and no email, and the model cannot introspect its
        own account, so the credential is the single honest source.

        The ACCESS token is authoritative: it is what the request goes out under, and a refresh rotates
        it, so where the tokens could ever drift it is the one that describes the spend. Other tokens
        contribute a field only when their own account id agrees with it — an account id from one token
        beside an email from another would name an account that need not exist, and --account would then
        accept an address the credential does not actually spend under."""
        auth = self._codex_creds()
        if auth is None:
            return None
        auth = _as_dict(auth)
        tok = _as_dict(auth.get("tokens"))
        access = _TokenIdentity.of(_jwt_claims(tok.get("access_token")))
        ident = _TokenIdentity.of(_jwt_claims(tok.get("id_token")))
        bare_id = _str_or_none(tok.get("account_id"))

        # Authoritative account id: the access token's, else the id token's, else the bare field.
        account_id = access.account_id or ident.account_id or bare_id
        seen_ids = {v.lower() for v in (access.account_id, ident.account_id, bare_id) if v}
        conflict = len(seen_ids) > 1  # two tokens naming different accounts ⇒ we refuse to pick

        def _corroborated(field: str) -> tuple[str | None, bool]:
            """The field's value from every token that is talking about `account_id`, and whether those
            tokens disagree. A token with no account id of its own cannot be correlated, so it is not
            consulted: silence is safer than an uncheckable claim."""
            vals = [
                getattr(t, field)
                for t in (access, ident)
                if getattr(t, field) and t.account_id and account_id and t.account_id.lower() == account_id.lower()
            ]
            # With no account id anywhere there is nothing to correlate against, and exactly one token
            # speaking is unambiguous — the id-token-only credential.
            if not account_id:
                vals = [getattr(t, field) for t in (access, ident) if getattr(t, field)]
            return (vals[0] if vals else None, len({v.lower() for v in vals}) > 1)

        email, email_conflict = _corroborated("email")
        plan, _ = _corroborated("plan")
        return CodexAccount(
            auth_mode=_str_or_none(auth.get("auth_mode")),
            account_id=account_id,
            email=email,
            plan=plan,
            conflict=conflict or email_conflict,
        )

    def _codex_creds_source(self) -> Path:
        """The credential location the OPERATOR must edit to change this worker's Codex account. Under an
        isolated home that is NOT the file we read: we read a mirror, and isolate_home leaves a marker
        naming the real one. Telling an isolated worker's operator to re-login into the mirror would send
        them to a file mirror_creds() overwrites from the original on the next round."""
        d = codex_dir(self.cfg.home)
        src = _read_marker(d / ".tauceti-creds-source")
        return Path(src) if src else d

    def codex_account_problem(self, requested: str) -> str | None:
        """None if this worker's Codex credential is the account the operator asked for; otherwise the
        message to fail with. Actionable by construction: every branch ends in a command to run, because
        the operator hitting this cannot see which account they are on by any other means."""
        # Sync from the operator's real credential FIRST. Under an isolated home we otherwise read a
        # mirror that run_round only refreshes later, so an operator who has just switched accounts
        # would be told to switch to the account they are already on — the worst possible false alarm
        # for a check whose entire value is being believed.
        mirror_creds(self.cfg)
        acct = self.codex_account()
        src = self._codex_creds_source()
        iso = codex_dir(self.cfg.home)

        # codex consults CODEX_API_KEY / CODEX_ACCESS_TOKEN BEFORE its persisted credential store, and
        # TauCeti's agent launcher clears only OPENAI_API_KEY. Either variable would therefore let the
        # round spend an account we never inspected, while --account reported a clean pass. There is no
        # honest way to check them (an opaque key names no account), so refuse to certify anything.
        for var in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            if os.environ.get(var, "").strip():
                return _para(
                    f"--account {requested}, but ${var} is set in this environment. codex reads that "
                    f"ahead of {src}/auth.json, so the round could spend a credential TauCeti cannot "
                    f"identify and did not check. Unset ${var} and re-run, so the account named by the "
                    f"credential file is the account that actually pays."
                )
        # macOS + an isolated home is the one combination where the file we check is NOT the file the
        # round spends: mirror_creds() returns early on Darwin, so the once-copied mirror never re-syncs
        # and can name an account the operator has since switched away from. Refuse rather than certify
        # a file we know may be stale.
        if sys.platform == "darwin" and iso != src:
            return _para(
                f"--account {requested} cannot be verified for this worker: on macOS an isolated "
                f"worker's Codex credential ({iso}/auth.json) is copied once from {src} and never "
                f"re-synced, so TauCeti cannot tell whether it still names the account you asked for. "
                f"Run this worker without an isolated home (the 'default' --worker-id, no "
                f"--isolate-home), or point $CODEX_HOME at the credential directly."
            )

        # Every command we print names the credential store it acts on, ALWAYS — even when that is the
        # default ~/.codex. A bare `codex logout` targets whatever store the reader's shell resolves to,
        # so an operator running under a custom or per-worker $CODEX_HOME who copy-pastes it would revoke
        # an unrelated account's session: the exact harm this message is warning them about. We cannot
        # reliably tell whether src IS their default either — under an isolated home $HOME has already
        # been repointed, so Path.home() is the worker's, not the operator's. Being explicit is never
        # wrong; guessing sometimes is.
        pfx = f"CODEX_HOME={shlex.quote(str(src))} "

        def switch_with(lead: str, *, logout: bool = True) -> str:
            """The switch instructions under a branch-appropriate lead-in — "switch" is the wrong verb
            when there is no credential yet, and `logout` has nothing to revoke then either."""
            return (
                _para(lead)
                + "\n\n"
                + (f"    {pfx}codex logout\n" if logout else "")
                + f"    {pfx}codex login       # sign in as {requested}\n\n"
                + _para(
                    f"Codex has no account picker — it completes as whichever ChatGPT account your "
                    f"browser is already signed into. If that is not {requested}, sign out of "
                    f"chatgpt.com first, or paste the URL codex prints into a private window."
                )
                + "\n\n"
                + _para("Then re-run TauCeti; this check will confirm it took.")
            )

        isolated = (
            _para("To keep both accounts on this machine you can use an isolated $CODEX_HOME, like this:")
            + "\n\n"
            + f"    CODEX_HOME=~/.codex-tauceti codex login       # sign in as {requested}\n"
            + f"    CODEX_HOME=~/.codex-tauceti tauceti work --agent codex --account {requested}"
        )
        if acct is None:
            # _read_json_file conflates "absent" with "unreadable or not JSON". Distinguish them here:
            # telling someone to log in when the file is right there but corrupt sends them to the
            # wrong remedy, and quietly logging in over a corrupt file could destroy a live session.
            if _safe_exists(codex_dir(self.cfg.home) / "auth.json"):
                return _para(
                    f"--account {requested}, but the Codex credential at {src}/auth.json could not be "
                    f"read — it is unreadable or not valid JSON, so TauCeti cannot tell which account "
                    f"it would spend under. Check its permissions and contents before re-running; if it "
                    f"is genuinely corrupt, `{pfx}codex login` will rewrite it."
                )
            return (
                _para(f"--account {requested}, but there is no Codex credential at {src}.")
                + "\n\n"
                + switch_with("To authenticate this machine's Codex:", logout=False)
            )
        if not acct.describes_an_account:
            # An API-key credential is a valid way to run codex, but it carries no ChatGPT account
            # identity, so --account cannot be honoured either way. Say that, rather than reporting a
            # mismatch against an account that does not exist.
            kind = "an API key" if acct.auth_mode == "apikey" else f"a {acct.auth_mode or 'unrecognised'} credential"
            return (
                _para(
                    f"--account {requested}, but {src}/auth.json holds {kind}, which carries no ChatGPT "
                    f"account identity — --account cannot be verified against it."
                )
                + "\n\n"
                + switch_with("To run under a named ChatGPT account instead:")
            )
        if acct.conflict:
            return (
                _para(
                    f"--account {requested}, but the identity claims in {src}/auth.json disagree with "
                    f"each other, so TauCeti cannot say which account it would spend under. That is what "
                    f"a half-written credential looks like — if something was rotating it, let that "
                    f"finish before re-running."
                )
                + "\n\n"
                + switch_with("Otherwise, re-authenticate:")
            )
        if acct.matches(requested):
            return None
        return (
            _para(
                f"--account {requested}, but {src}/auth.json is authenticated as {acct.describe()}. "
                f"TauCeti will not switch accounts for you."
            )
            + "\n\n"
            + switch_with("To switch this machine's Codex account:")
            + "\n\n"
            + _para(
                f"Note `codex logout` revokes {acct.email or acct.account_id}'s session, so you will "
                f"have to sign in again to use it."
            )
            + f"\n\n{isolated}"
        )

    def codex_account_fingerprint(self) -> str | None:
        """Stable, non-secret cache identity for the Codex account currently used by this worker."""
        auth = self._codex_creds() or {}
        tok = auth.get("tokens") or {}
        return self._fingerprint(self._codex_account_id(auth) or tok.get("access_token"))

    def codex(self, *, refresh: bool = False) -> Provider:
        mirror_creds(self.cfg)  # re-sync the isolated copy from the operator's fresh file
        auth = self._codex_creds()
        if not auth:
            return Provider("codex", False, None, error="no ~/.codex/auth.json")
        # Prefer the stable account id so a same-account token refresh keeps the cache; fall back to the
        # token itself when no id is available.
        fp = self._codex_account_id(auth) or self._fingerprint((auth.get("tokens") or {}).get("access_token"))
        cached = self._cached_raw("codex", fp)
        payload = None if refresh else cached
        if payload is None:
            tok = (auth.get("tokens") or {}).get("access_token")
            if not tok:
                return Provider("codex", False, None, error="no codex access_token")
            headers = {"Authorization": f"Bearer {tok}", "User-Agent": "codex-cli"}
            acct = self._codex_account_id(auth)
            if acct:
                headers["ChatGPT-Account-Id"] = acct
            try:
                code, payload, retry_after = _http_get_json(CODEX_USAGE_URL, headers)
            except GitHubError as e:
                if refresh and cached is not None:
                    return self._codex_from_payload(cached)
                return Provider("codex", False, None, error=str(e))
            # The worker never refreshes (the operator owns the single-use refresh token). On expiry the
            # access token simply reads as unavailable until the operator's external refresher rotates it
            # and mirror_creds picks it up next cycle.
            if code != 200 or not payload:
                if refresh and cached is not None and code != 401:
                    return self._codex_from_payload(cached)
                err = "codex token expired; refresh left to the operator" if code == 401 else f"codex usage HTTP {code}"
                return Provider("codex", False, None, error=err, retry_after=retry_after)
            self._store_raw("codex", payload, fp)
        return self._codex_from_payload(payload)

    def _codex_from_payload(self, payload: dict) -> Provider:
        rl = payload.get("rate_limit") or {}
        limit_reached = bool(rl.get("limit_reached"))
        wins = []
        for key in ("primary_window", "secondary_window"):
            w = rl.get(key)
            # An explicitly null / absent window does not apply to this account, so SKIP it — do not emit
            # an 'unknown' window that would fail-closed on a window that simply isn't there. The codex
            # usage endpoint dropped the short `secondary_window` for pro accounts, now reporting a single
            # weekly window in `primary_window` with the other null; the old positional parse read that
            # null as 'usage unknown' and pinned codex out.
            if w is None:
                continue
            # A window that IS present but is not an object is malformed, NOT 'not applicable' — coerce it to
            # an empty dict so it falls through as an 'unknown' window (fail-closed) rather than being silently
            # dropped, which would let a garbage value read as confirmed-under-pace. A present-but-well-formed
            # window that merely drops `used_percent` also classifies as 'unknown' below (the schema-drift guard).
            if not isinstance(w, dict):
                w = {}
            lim = w.get("limit_window_seconds")
            ra = w.get("reset_after_seconds")
            elapsed = None
            resets = None
            if isinstance(lim, (int, float)) and lim > 0 and isinstance(ra, (int, float)):
                elapsed = (lim - ra) / lim * 100
                resets = time.time() + max(0, ra)
            # Name by the window's own length, not its position: the endpoint has reordered which slot
            # carries the ~5h vs the ~7d window, and a positional label would call a weekly window 'session'.
            name = "session" if isinstance(lim, (int, float)) and lim <= 24 * 3600 else "weekly"
            wins.append(_classify_window(name, w.get("used_percent"), elapsed, resets, limit_reached))
        # No usable window at all ⇒ fail-closed (unavailable), never fail-OPEN on an empty all(...) that
        # is vacuously True.
        avail = bool(wins) and all(x.status == "under-pace" for x in wins) and not limit_reached
        nxt = self._next_eligible(wins)
        return Provider("codex", avail, "gpt-5" if avail else None, wins, None, nxt)

    # --- Claude ------------------------------------------------------------
    def _claude_creds(self) -> tuple[dict | None, bool]:
        """Returns (oauth_block, from_keychain). The block is Claude Code's {accessToken, refreshToken,
        expiresAt, ...}. On macOS the login Keychain is authoritative (the CLI stores creds there, not in
        a file), so we read it FIRST and only fall back to a file; from_keychain marks creds we must NOT
        refresh/persist, since rotating the shared token would log out the operator's claude. Reading the
        Keychain first also means a credentials file we materialize for the bubble never shadows the live
        Keychain here. On Linux the file is the store."""
        if sys.platform == "darwin":
            kc = _claude_keychain_creds()
            if kc and kc.get("claudeAiOauth"):
                return kc.get("claudeAiOauth"), True
        d = _read_json_file(claude_dir(self.cfg.home) / ".credentials.json")
        block = d.get("claudeAiOauth") if d else None
        if block:
            # On macOS a file is only a Keychain mirror sharing the one refresh token, so it must never be
            # refreshed even when the Keychain read failed; on Linux the file is the store and refreshable.
            return block, sys.platform == "darwin"
        return None, False

    def claude(self, *, refresh: bool = False) -> Provider:
        """Read the Claude usage endpoint and report whether opus may run. PURE: it reads, it never
        spends. A dashboard refresh, `tauceti status`, and an `auto` selection that ends up picking
        codex must all be able to call this without making a Claude request.

        The state machine, per window and independent of the sibling window (they reset on separate
        clocks, so neither may be inferred from the other):

          active                 → paced normally, and both windows gate. Only STRICTLY under the pace
                                   budget counts as launchable; sitting on the budget is a soft block.
          absent / malformed     → HARD block naming the window and the offending condition. An
                                   unreadable quota constraint is not the same as no constraint.
          idle (recognized       → HARD block, and possibly `bootstrap_eligible`: the one condition a
          post-reset gap)          controlled side effect can clear. Acting on it is the caller's
                                   explicit decision (authorize_claude_launch), never a read's.
        """
        mirror_creds(self.cfg)  # re-sync the isolated copy from the operator's fresh file
        oauth, _from_keychain = self._claude_creds()
        if not oauth:
            err = f"no {claude_dir(self.cfg.home) / '.credentials.json'}"
            if sys.platform == "darwin":
                err += ' (and no "Claude Code-credentials" Keychain entry)'
            return Provider("claude", False, None, error=err)
        # The cache is keyed by the access token: a rotation to a different account changes it (forcing a
        # re-fetch); an external same-account refresh also changes it, costing one harmless extra fetch.
        # The bootstrap RESERVATION deliberately does not use this — see _claude_account_key.
        fp = self._fingerprint(oauth.get("accessToken"))
        prov, _readings = self._claude_pass(fp, oauth.get("accessToken"), refresh=refresh)
        return prov

    def authorize_claude_launch(self) -> Provider:
        """The launch stage: the ONLY place a Claude request may be spent on initializing a window.

        Callers must have established, before calling, that there is concrete work to do, that the
        non-model preflight passes, and that Claude is the provider selected for that work. This
        method enforces what remains — that the sole unresolved condition is one or more recognized
        idle windows, that every other window is active WITH headroom, and that the operator's pace
        curve permits initializing a fresh window at all.

        The request is taken under a durable, cross-worker reservation, so a crash between the request
        and its outcome cannot license a second one. Afterwards the cache is dropped and a FRESH usage
        response decides: a bootstrap never grants availability by itself."""
        prov = self.claude()
        if prov.available or not prov.bootstrap_eligible:
            return prov  # nothing to do, or something other than an initializable idle window blocks
        windows = list(prov.pending_bootstrap)
        # Freeze the identity across reserve/request/settle. A request can cross a wall-clock period
        # boundary (or credential lookup can transiently fail); recomputing would orphan the original
        # in-progress claim and record its outcome against a different episode.
        episode_keys = [self._episode_key(w) for w in windows]
        held, why = self._reserve_bootstrap(episode_keys)
        if not held:
            return self._reannotate(prov, windows, why)
        log(
            f"claude quota: {'/'.join(windows)} window reset and not yet initialized — sending one small "
            f"bootstrap request to open it"
        )
        ok, detail = self._claude_bootstrap_request()
        self._settle_bootstrap(episode_keys, ok, detail)
        self._forget_raw("claude")  # whatever we cached predates the request meant to change it
        if not ok:
            log(f"claude quota: bootstrap request failed ({detail}) — claude stays unavailable")
        return self.claude()  # the fresh telemetry decides, including whether there is headroom to spend

    def _claude_pass(
        self, fp: str | None, tok: str | None, *, refresh: bool = False
    ) -> tuple[Provider, list[Reading] | None]:
        """One read of the usage endpoint (cache-aware) turned into a Provider. Returns readings=None
        when no payload was obtained at all, so the caller can tell "the endpoint would not answer"
        from "the endpoint answered something we could not use"."""
        cached = self._cached_claude(fp)
        payload = None if refresh else cached
        if payload is None:
            if not tok:
                return Provider("claude", False, None, error="no claude accessToken"), None
            headers = {"Authorization": f"Bearer {tok}", "anthropic-beta": CLAUDE_BETA, "User-Agent": "claude-code/2.1"}
            try:
                code, payload, retry_after = _http_get_json(CLAUDE_USAGE_URL, headers)
            except GitHubError as e:
                if refresh and cached is not None:
                    readings = _claude_readings(cached)
                    notes, bootstrap_recorded = self._idle_notes(readings)
                    return self._claude_provider(readings, notes, bootstrap_recorded=bootstrap_recorded), readings
                return Provider("claude", False, None, error=str(e)), None
            # The worker never refreshes: the operator owns the single-use refresh token (rotating it here
            # would invalidate their copy). An expired access token reads as unavailable until the operator's
            # external refresher rotates it and mirror_creds picks it up next cycle. (On macOS the keychain-
            # first read above already means we never hold a file refresh token to rotate.) Always name the
            # status code — an auth failure, a rate-limited endpoint and a server error are different
            # problems with different fixes, and none of them is "usage unknown".
            if code != 200 or not payload:
                if refresh and cached is not None and code != 401:
                    readings = _claude_readings(cached)
                    notes, bootstrap_recorded = self._idle_notes(readings)
                    return self._claude_provider(readings, notes, bootstrap_recorded=bootstrap_recorded), readings
                err = f"claude usage HTTP {code}"
                if code == 401:
                    err += " (token expired; refresh left to the operator)"
                elif code == 429:
                    err += " (usage endpoint rate-limited)"
                elif code == 200:
                    err = "claude usage response empty"
                return Provider("claude", False, None, error=err, retry_after=retry_after), None
            # A response that is not a JSON object is not a quota verdict — and .get() on it would raise
            # inside the pacer. Name it and fail closed.
            problem = _claude_payload_problem(payload)
            if problem:
                return Provider("claude", False, None, error=problem, retry_after=retry_after), None
            readings = _claude_readings(payload)
            # Cache ONLY a payload that is fully resolved, and only until the first reset it describes.
            # An idle/absent/malformed payload is never pinned: re-reading it is the only thing that can
            # resolve it, and one extra fetch per poll is the cheapest part of this system.
            valid_until = _claude_valid_until(readings)
            if valid_until is not None:
                self._store_raw("claude", payload, fp, valid_until)
        else:
            readings = _claude_readings(payload)
        notes, bootstrap_recorded = self._idle_notes(readings)
        return self._claude_provider(readings, notes, bootstrap_recorded=bootstrap_recorded), readings

    def _cached_claude(self, fp: str | None) -> dict | None:
        """A cached usage payload, or None when it must not be served. Validity is BOTH conditions:
        the payload is fully and safely interpretable, and the wall clock has not yet reached any reset
        it represents. `_cached_raw` enforces the stored expiry; re-deriving it here also covers an
        entry written before this rule existed (and any payload whose windows no longer parse). A
        cached body that is not a JSON object is discarded outright, exactly like a fresh one."""
        payload = self._cached_raw("claude", fp)
        if payload is None or _claude_payload_problem(payload) is not None:
            return None
        if _claude_valid_until(_claude_readings(payload)) is None:
            return None
        return payload

    def _claude_provider(
        self,
        readings: list[Reading],
        notes: dict[str, str] | None = None,
        *,
        bootstrap_recorded: bool = False,
    ) -> Provider:
        """Both windows always gate: a window we cannot read blocks exactly as hard as one that is
        exhausted. `notes` supplies the bootstrap phase for idle windows.

        `bootstrap_eligible` is decided here, purely, so that every caller sees the same verdict and
        none of them has to act on it. It requires ALL of: at least one idle window; every non-idle
        window active with positive headroom (a sibling at budget, over pace, exhausted, absent or
        malformed forbids it — we would be spending against a constraint we cannot honour or cannot
        read); and a pace curve under which a fresh window has budget at all."""
        notes = notes or {}
        wins = [_window_from_reading(r, notes.get(r.window)) for r in readings]
        err = None
        if wins and all(w.status == STATE_ABSENT for w in wins):
            # Not one recognizable record in the whole body: this is not a quota reading at all — the
            # schema moved, or we were handed something that isn't the usage response.
            err = "claude usage response schema unsupported (no session or weekly record)"
        avail = bool(wins) and all(w.status == STATUS_UNDER_PACE for w in wins)
        idle = [w.name for w in wins if w.status == STATE_IDLE]
        others_ok = all(w.status == STATUS_UNDER_PACE for w in wins if w.status != STATE_IDLE)
        # A live ledger record means this episode already has a request in flight or completed. Keep
        # the parent loop asleep as well as refusing the launch-stage reservation: otherwise it would
        # repeatedly pay for a deep GitHub survey only to discover the same reservation again.
        eligible = bool(idle) and others_ok and not bootstrap_recorded and _idle_init_block() is None
        return Provider(
            "claude",
            avail,
            "opus" if avail else None,
            wins,
            err,
            self._next_eligible(wins),
            None,
            eligible,
            idle if eligible else [],
        )

    def _claude_from_payload(self, payload: dict) -> Provider:
        """Provider straight from a payload, with no cache, reservation or bootstrap — the pure parse."""
        return self._claude_provider(_claude_readings(payload))

    def _reannotate(self, prov: Provider, windows: list[str], note: str) -> Provider:
        """Re-render `prov` with a different account of its idle windows (e.g. why a reservation was
        refused), leaving every other window untouched."""
        wins = [
            Window(w.name, w.used, w.elapsed, w.resets_at, w.status, w.budget, note)
            if (w.name in windows and w.status == STATE_IDLE)
            else w
            for w in prov.windows
        ]
        return Provider("claude", False, None, wins, prov.error, prov.next_eligible, prov.retry_after, False, [])

    # --- the bounded post-reset bootstrap -----------------------------------
    def _idle_notes(self, readings: list[Reading]) -> tuple[dict[str, str], bool]:
        """What to say about each idle window, given what the shared ledger remembers about this idle
        episode. These are the states an operator needs to tell apart: nothing tried yet (and whether
        their own pace curve is what forbids trying), a request that went through but whose window
        still isn't reporting, and a request that failed outright."""
        notes = {}
        recorded = False
        ledger = self._read_bootstrap_ledger()
        for r in readings:
            if r.state != STATE_IDLE:
                continue
            rec = ledger.get(self._episode_key(r.window))
            if not isinstance(rec, dict):
                notes[r.window] = _idle_phrase(r)
                continue
            recorded = True
            if rec.get("state") == "in-progress":
                notes[r.window] = "bootstrap in progress (another worker holds the reservation)"
            elif rec.get("ok"):
                notes[r.window] = "bootstrap attempted; awaiting fresh usage"
            else:
                notes[r.window] = f"bootstrap failed: {rec.get('detail') or 'unknown error'}"
        return notes, recorded

    # The reservation lives beside the CREDENTIALS, not under state/<worker-id>/: every worker measuring
    # the same Claude account must contend for the same record, including workers with isolated $HOMEs
    # (which record where their credentials were seeded from) and workers from other checkouts.
    def _claude_creds_source(self) -> Path:
        """The canonical credential location this worker is measuring — the operator's real claude dir
        even when running under an isolated $HOME (isolate_home leaves a .tauceti-creds-source marker)."""
        d = claude_dir(self.cfg.home)
        src = _read_marker(d / ".tauceti-creds-source")
        return Path(src) if src else d

    def _claude_account_key(self) -> str:
        """A stable identity for the Claude ACCOUNT, used to scope the shared bootstrap reservation.

        Deliberately NOT the access-token fingerprint the cache uses: an ordinary token refresh rotates
        that without changing either the account or the reset episode, which would silently hand every
        worker a fresh bootstrap allowance. We prefer a real account identifier when the credential
        exposes one; Claude Code's oauth blob usually does not, in which case we fall back to the
        canonical credential-source PATH. LIMITATION of that fallback: it serializes workers that share
        a credential file (the fleet case this exists for), but two hosts with separate copies of the
        same account cannot see each other's reservations and may each spend one bootstrap request."""
        oauth, _kc = self._claude_creds()
        for key in ("accountUuid", "account_uuid", "accountId", "organizationUuid", "emailAddress"):
            val = (oauth or {}).get(key)
            if isinstance(val, str) and val:
                return f"account:{val}"
        for key in ("account", "organization"):
            blk = (oauth or {}).get(key)
            if isinstance(blk, dict):
                for sub in ("uuid", "id", "email_address"):
                    if isinstance(blk.get(sub), str) and blk[sub]:
                        return f"account:{blk[sub]}"
        return f"creds:{self._claude_creds_source()}"

    def _episode_key(self, window: str) -> str:
        """Identify the idle EPISODE a reservation covers: the account, the window kind, and which
        window-length period of the clock we are in.

        An idle window reports no reset clock, so it cannot name its own episode; the period index is
        derived from the window's own fixed length, which is the same figure every worker computes.
        What this buys, honestly stated: at most one bootstrap request per account per window-length
        period, across workers and across crashes. An idle window straddling a period boundary can cost
        a second request — bounded, and far from the unbounded per-poll spend a request-then-record
        sequence allows."""
        period = int(time.time() // _WINDOW_S[window])
        return f"{self._claude_account_key()}|{window}|{period}"

    def _bootstrap_paths(self) -> tuple[Path, Path]:
        """(ledger, lock) for the shared reservation."""
        d = self._claude_creds_source() / CLAUDE_QUOTA_DIRNAME
        return d / CLAUDE_BOOTSTRAP_FILE, d / "bootstrap.lock"

    @contextmanager
    def _bootstrap_lock(self):
        """Exclusive, cross-process hold on the shared reservation, or None when it cannot be taken.
        A lock we cannot take or a directory we cannot create yields None and the caller FAILS CLOSED:
        an unenforceable reservation must not license a request."""
        ledger, lockpath = self._bootstrap_paths()
        fd = None
        try:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lockpath, os.O_CREAT | os.O_WRONLY, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            yield None
            return
        try:
            yield ledger
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass

    def _read_bootstrap_ledger(self) -> dict:
        """The shared ledger's live records (stale in-progress reservations dropped). Read without the
        lock: a torn read degrades to 'no record', and every decision that MATTERS re-reads under it."""
        ledger, _lock = self._bootstrap_paths()
        d = _read_json_file(ledger)
        if not isinstance(d, dict):
            return {}
        now = time.time()
        live = {}
        for key, rec in d.items():
            if not isinstance(rec, dict):
                continue
            age = now - (rec.get("at") or 0)
            if rec.get("state") == "in-progress":
                # A worker that died mid-request leaves this behind; let it expire, generously, so the
                # window is retried eventually but never while a real request may still be in flight.
                if age < CLAUDE_BOOTSTRAP_TIMEOUT_S + CLAUDE_BOOTSTRAP_STALE_MARGIN_S:
                    live[key] = rec
            elif age < CLAUDE_BOOTSTRAP_RETRY_S:
                live[key] = rec
        return live

    def _reserve_bootstrap(self, episode_keys: list[str]) -> tuple[bool, str]:
        """Claim the right to make ONE bootstrap request for these windows, BEFORE making it.

        The reservation is written and fsynced under an exclusive lock, so a crash between the claim and
        the request cannot be mistaken for 'nobody has tried' — the opposite ordering (request first,
        record after) cannot bound anything, because the request is the part that survives the crash.
        Returns (held, why-not)."""
        with self._bootstrap_lock() as ledger:
            if ledger is None:
                return False, "bootstrap reservation unavailable (cannot lock the shared ledger)"
            live = self._read_bootstrap_ledger()
            for key in episode_keys:
                rec = live.get(key)
                if isinstance(rec, dict):
                    if rec.get("state") == "in-progress":
                        return False, "bootstrap in progress (another worker holds the reservation)"
                    if rec.get("ok"):
                        return False, "bootstrap attempted; awaiting fresh usage"
                    return False, f"bootstrap failed: {rec.get('detail') or 'unknown error'}"
            claimed = dict(live)
            for key in episode_keys:
                claimed[key] = {"state": "in-progress", "at": int(time.time())}
            try:
                _write_json_atomic(ledger, claimed)  # temp file + fsync + atomic rename
            except OSError as e:
                return False, f"bootstrap reservation unavailable (cannot record it: {e})"
        return True, ""

    def _settle_bootstrap(self, episode_keys: list[str], ok: bool, detail: str) -> None:
        """Replace the in-progress reservation with its outcome."""
        with self._bootstrap_lock() as ledger:
            if ledger is None:
                return  # the reservation expires on its own; we never re-request while it is live
            records = self._read_bootstrap_ledger()
            for key in episode_keys:
                records[key] = {
                    "state": "done",
                    "at": int(time.time()),
                    "ok": bool(ok),
                    "detail": detail,
                }
            try:
                _write_json_atomic(ledger, records)
            except OSError:
                pass

    def _bootstrap_spec(self) -> tuple[list[str], dict, str]:
        """(argv, env, cwd) for the bootstrap request — built separately from running it so the
        isolation it depends on is directly testable.

        cwd is a genuine throwaway temp directory OUTSIDE any checkout: the working directory is what
        decides which CLAUDE.md, settings, hooks and MCP config a `claude` turn loads, and the point of
        this request is to be one minimal subscription-authenticated turn and nothing else.

        The environment keeps exactly what identifies the account being measured — $HOME and
        $CLAUDE_CONFIG_DIR, so the request bills the same credentials the pacer just read — and drops
        every alternative authentication or provider route. Measuring one account while spending
        another (or an API key, or Bedrock/Vertex/Foundry) would make the whole reading meaningless."""
        import shlex
        import tempfile

        argv = [*(shlex.split(CLAUDE_CMD) or ["claude"]), "-p", CLAUDE_BOOTSTRAP_PROMPT]
        env = {k: v for k, v in os.environ.items() if k not in CLAUDE_BOOTSTRAP_DROP_ENV}
        return argv, env, tempfile.mkdtemp(prefix="tauceti-quota-bootstrap-")

    def _claude_bootstrap_request(self) -> tuple[bool, str]:
        """ONE small `claude -p` turn, purely to open a window the endpoint reports as reset but not
        yet initialized. Deliberately the smallest real request we can make, not a round: a round is
        unbounded spend against a quota window we currently cannot read.

        It doubles as a safety check — if the window has NOT actually reset, this request is refused by
        the provider, we record the failure and stay blocked. So the bootstrap can never unlock
        spending by itself; it can only produce telemetry, and the fresh telemetry decides."""
        argv, env, cwd = self._bootstrap_spec()
        try:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_BOOTSTRAP_TIMEOUT_S,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"{argv[0]} timed out after {CLAUDE_BOOTSTRAP_TIMEOUT_S}s"
        except FileNotFoundError:
            return False, f"{argv[0]} not found on PATH"
        except OSError as e:
            return False, f"could not run {argv[0]}: {e}"
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        if p.returncode != 0:
            return False, f"{argv[0]} exited {p.returncode}{_tail_detail(p.stderr or p.stdout)}"
        return True, "ok"

    @staticmethod
    def _next_eligible(windows: list[Window]) -> float | None:
        # Earliest reset among windows that are currently blocking on TIME (at budget, over pace, or
        # exhausted) — the ones a wait actually fixes.
        blocking = (STATUS_AT_BUDGET, STATUS_OVER_PACE, STATUS_EXHAUSTED)
        blocked = [w.resets_at for w in windows if w.status in blocking and w.resets_at]
        return min(blocked) if blocked else None

    # --- selection ---------------------------------------------------------
    def choose(self, forced: str | None, *, refresh: bool = False) -> tuple[str | None, dict]:
        """Return (agent_to_run_now or None, {codex: Provider, claude: Provider}).

        forced in {codex, claude}: only that provider counts. None/'auto': codex preferred, opus
        fallback. OpenRouter agents (deepseek/minimax) bypass this entirely (handled by the caller).
        """
        snap = {}
        if forced in (None, "auto", "codex"):
            snap["codex"] = self.codex(refresh=refresh)
        if forced in (None, "auto", "claude"):
            snap["claude"] = self.claude(refresh=refresh)
        codex_ok = snap.get("codex") and snap["codex"].available
        opus_ok = snap.get("claude") and snap["claude"].available
        if forced == "codex":
            return ("codex" if codex_ok else None), snap
        if forced == "claude":
            return ("claude" if opus_ok else None), snap
        if codex_ok:
            return "codex", snap
        if opus_ok:
            return "claude", snap
        return None, snap


def _pace_reason(w: Window) -> str:
    """A soft pacing block, with the budget it met or exceeded (so a custom --pace is observable) and
    the quota still in hand. `at budget` and `ahead of pace` are different situations: the first is
    waiting for the budget line to rise, the second for usage to fall behind it again."""
    vs = "" if (w.used is None or w.budget is None) else f" (used {round(w.used)}% {{}} {round(w.budget)}% budget)"
    left = "" if w.used is None else f", {max(0, round(100 - w.used))}% left"
    if w.status == STATUS_AT_BUDGET:
        return f"{w.name} at budget{vs.format('=')}{left}"
    return f"{w.name} ahead of pace{vs.format('>')}{left}"


def _window_reason(w: Window) -> str:
    """One window's blocking condition, phrased to follow its name. Prefers the specific diagnosis the
    reader recorded ("weekly limit missing from usage response", "session reset timestamp invalid",
    "session window reset; awaiting initialization") over the generic fallback — an operator cannot act
    on "usage unknown", and the failures it used to cover need completely different responses."""
    if w.status == "exhausted":
        return f"{w.name} exhausted"
    return f"{w.name} {w.detail}" if w.detail else f"{w.name} usage unknown"


def _unavail_reason(prov: Provider) -> tuple[bool, str]:
    """Why an unavailable provider can't be used, and whether the block is *soft*.

    A soft block means there is real quota left and we're only pausing to pace the burn (over-pace) —
    distinct from a hard block where a window is exhausted, reset-but-uninitialized, missing or
    unreadable (all fail-closed). Returns (soft, reason)."""
    gating = prov.windows or []
    # Every hard condition is reported, and hard dominates a co-occurring over-pace window: we cannot
    # tell an unreadable window is not exhausted, so a partial payload (one window known and merely
    # ahead of pace, another unreadable) must NOT read as a soft pacing pause — under --ignore-quota
    # that would fire blind into a provider we can't confirm is up.
    hard = [w for w in gating if w.status in HARD_STATUSES]
    if hard:
        return False, "; ".join(_window_reason(w) for w in hard)
    paced = [w for w in gating if w.status in SOFT_STATUSES]
    if paced:
        return True, "; ".join(_pace_reason(w) for w in paced)
    return False, "unavailable"


def quota_line(snap: dict) -> str:
    """One-line quota summary from a {provider: Provider} snapshot."""
    parts = []
    for name in ("codex", "claude"):
        prov = snap.get(name)
        if prov is None:
            continue
        if prov.error:
            parts.append(f"{name} [yellow]?[/] ({prov.error})")
        elif prov.available:
            parts.append(f"{name} [green]✓[/] {prov.model}")
        else:
            soft, why = _unavail_reason(prov)
            glyph = "[yellow]~[/]" if soft else "[red]✗[/]"
            parts.append(f"{name} {glyph} ({why})")
    return "   ".join(parts) if parts else "quota: (not checked)"
