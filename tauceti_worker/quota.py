"""tauceti_worker.quota — the subscription pacer: read Codex/Claude usage and credentials and decide
which model may run now, plus the isolated-home credential mirroring."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
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

# The bootstrap request itself: the smallest useful `claude -p` turn. Its only job is to make ONE real
# request so the provider opens the window its usage endpoint says has reset.
CLAUDE_BOOTSTRAP_PROMPT = "Reply with the single word: ok"

CLAUDE_BOOTSTRAP_FILE = "quota-claude-bootstrap.json"


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

# Which reading wins when BOTH representations (the `limits` array and the legacy flat keys) describe
# the same window. A recognized `idle` outranks `malformed` because it is a positive statement by the
# endpoint ("this window is not open"), and it still blocks — it can only ever buy one small bootstrap
# request, never spending. `absent` ranks last: it is the absence of a statement, not a statement.
_STATE_RANK = {STATE_ACTIVE: 3, STATE_IDLE: 2, STATE_MALFORMED: 1, STATE_ABSENT: 0}

# Window statuses that block hard (fail-closed), as opposed to the soft over-pace throttle. `unknown`
# is the generic fail-closed status the codex reader still emits.
HARD_STATUSES = ("exhausted", STATE_IDLE, STATE_ABSENT, STATE_MALFORMED, "unknown")

# How a single JSON field read: omitted, explicitly null, a usable value, or garbage. Collapsing these
# is what let an invalid reset timestamp read the same as an explicit null (⇒ "fresh window").
_F_MISSING, _F_NULL, _F_VALUE, _F_BAD = "missing", "null", "value", "bad"

_ABSENT_REC = object()  # sentinel: this representation has no record for the window at all

_FLAT_KEY = {"session": "five_hour", "weekly": "seven_day"}


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
        return Window(name, used, e, resets_at, "exhausted")
    if not _finite_num(used) or e is None:
        return Window(name, used, e, resets_at, "unknown")
    u = max(0.0, min(100.0, used))
    thr = pace_budget(pace_curve(), e)  # max allowed used% at this elapsed%, per the operator's curve
    st = "exhausted" if u >= 100 else ("under-pace" if u <= thr else "over-pace")
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


def _claude_record_state(rec: dict, now: float) -> tuple[str, str | None, float | None, float | None]:
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
        return STATE_ACTIVE, None, used, resets
    # No reset clock stated at all (explicitly null, or omitted).
    if uk == _F_VALUE:
        if used > 0:
            return STATE_MALFORMED, f"usage {used:g}% reported with no reset clock", used, None
        return STATE_IDLE, None, used, None
    if _F_NULL in (uk, rk):
        return STATE_IDLE, None, None, None
    return STATE_MALFORMED, "record carries neither a usage figure nor a reset clock", None, None


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
    state, detail, used, resets = _claude_record_state(rec, now)
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
    """The best available reading for ONE window, judged independently of the other. Both the
    structured `limits` array and the legacy flat key are parsed, and the more informative reading
    wins (_STATE_RANK; the newer `limits` breaks ties). Nothing about the session's record may change
    how the weekly is read, or vice versa: the two windows reset on independent clocks."""
    now = time.time() if now is None else now
    candidates = (
        _claude_reading(window, "limits", _claude_limits_record(payload, window), now),
        _claude_reading(window, _FLAT_KEY[window], _claude_flat_record(payload, window), now),
    )
    return max(candidates, key=lambda r: _STATE_RANK[r.state])


def _claude_readings(payload: dict, now: float | None = None) -> list[Reading]:
    """The session and the overall (unscoped) weekly, each read on its own. These two gate the
    worker's opus; the per-model weekly caps do not."""
    now = time.time() if now is None else now
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
    """The default account of an idle window: it reset, and nothing has opened the next cycle yet."""
    return "window reset; awaiting initialization" + (f" ({r.detail})" if r.detail else "")


def _window_from_reading(r: Reading, note: str | None = None) -> Window:
    """Apply pacing to an `active` reading; carry every other state through verbatim so the caller can
    say what actually happened instead of a generic "usage unknown". Elapsed is derived from the fixed
    window length, since the endpoint gives a reset clock rather than an elapsed fraction."""
    if r.state == STATE_ACTIVE:
        window_s = SESSION_WINDOW_S if r.window == "session" else WEEK_WINDOW_S
        elapsed = (window_s - (r.resets_at - time.time())) / window_s * 100
        return _classify_window(r.window, r.used, elapsed, r.resets_at, False)
    detail = note or (_idle_phrase(r) if r.state == STATE_IDLE else r.detail)
    return Window(r.window, r.used, None, r.resets_at, r.state, None, detail)


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
    Keychain is the store; the keychain-first pacer and _ensure_claude_creds_for_bubble handle it). Safe to
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
    src_codex = _read_marker(cfg.home / ".codex" / ".tauceti-creds-source")
    if src_codex:  # absent on homes seeded before this marker existed
        _mirror_creds_file(
            Path(src_codex) / "auth.json",
            cfg.home / ".codex" / "auth.json",
            block_key="tokens",
            tok_key="access_token",
            rt_key="refresh_token",
            rt_placeholder=CODEX_RT_PLACEHOLDER,
        )


class Quota:
    """The pacer. `bootstrap` says whether this caller is allowed to SPEND to break a post-reset
    deadlock: the loop and the round resolver set it (they are about to launch work anyway), while
    read-only callers — `tauceti status`, the dashboard's 90-second refresh — leave it off and just
    report the state they found. Nothing else about the decision differs between the two."""

    def __init__(self, cfg: Config, *, bootstrap: bool = False):
        self.cfg = cfg
        self.cache_dir = cfg.quota_cache
        self.bootstrap = bootstrap

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
        return _read_json_file(self.cfg.home / ".codex" / "auth.json")

    def _codex_account_id(self, auth: dict) -> str | None:
        tok = auth.get("tokens") or {}
        if tok.get("account_id"):
            return tok["account_id"]
        idt = tok.get("id_token")
        if idt and idt.count(".") == 2:
            try:
                payload = idt.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                return claims.get("chatgpt_account_id") or claims.get("account_id")
            except Exception:
                return None
        return None

    def codex(self) -> Provider:
        mirror_creds(self.cfg)  # re-sync the isolated copy from the operator's fresh file
        auth = self._codex_creds()
        if not auth:
            return Provider("codex", False, None, error="no ~/.codex/auth.json")
        # Prefer the stable account id so a same-account token refresh keeps the cache; fall back to the
        # token itself when no id is available.
        fp = self._codex_account_id(auth) or self._fingerprint((auth.get("tokens") or {}).get("access_token"))
        payload = self._cached_raw("codex", fp)
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
                return Provider("codex", False, None, error=str(e))
            # The worker never refreshes (the operator owns the single-use refresh token). On expiry the
            # access token simply reads as unavailable until the operator's external refresher rotates it
            # and mirror_creds picks it up next cycle.
            if code != 200 or not payload:
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

    def claude(self) -> Provider:
        """Read the Claude usage endpoint and decide whether opus may run.

        The state machine, per window and independent of the sibling window (they reset on separate
        clocks, so neither may be inferred from the other):

          active                 → paced normally, and both windows gate.
          absent / malformed     → HARD block naming the window and the offending condition. An
                                   unreadable quota constraint is not the same as no constraint.
          idle (recognized       → HARD block too, but exactly once it authorizes ONE small `claude -p`
          post-reset gap)          request, which is the only thing that can open the new window. The
                                   attempt is written to a ledger, the cached payload is dropped, and a
                                   FRESH usage response decides. Telemetry that is still idle afterwards
                                   reports "bootstrap attempted"/"bootstrap failed" and stays blocked.

        That breaks the fixed-point deadlock (the worker won't launch claude while a window reads
        unreadable, and only launching claude opens the window) without ever granting a full round on
        the strength of missing telemetry."""
        mirror_creds(self.cfg)  # re-sync the isolated copy from the operator's fresh file
        oauth, _from_keychain = self._claude_creds()
        if not oauth:
            err = f"no {claude_dir(self.cfg.home) / '.credentials.json'}"
            if sys.platform == "darwin":
                err += ' (and no "Claude Code-credentials" Keychain entry)'
            return Provider("claude", False, None, error=err)
        # No stable account id is exposed in the oauth block, so fingerprint the access token: a rotation to
        # a different account changes it (forcing a re-fetch); an external same-account refresh also changes
        # it, which only costs one harmless extra fetch every several hours.
        fp = self._fingerprint(oauth.get("accessToken"))
        tok = oauth.get("accessToken")
        prov, readings = self._claude_pass(fp, tok)
        if readings is None:
            return prov  # no payload at all (no token / HTTP / network) — nothing a bootstrap could fix
        # A window whose record already carries a bootstrap attempt is NOT retried: that is what bounds
        # this to one request per reset, however often the loop polls.
        todo = [r.window for r in readings if r.state == STATE_IDLE and not self._bootstrap_record(fp, r.window)]
        if not (self.bootstrap and todo):
            return prov  # read-only caller, or an attempt is already on record — `prov` says which
        log(
            f"claude quota: {'/'.join(todo)} window reset and not yet initialized — sending one small "
            f"bootstrap request to open it"
        )
        ok, detail = self._claude_bootstrap_request()
        self._remember_bootstrap(fp, todo, ok, detail)
        self._forget_raw("claude")  # whatever we cached predates the request meant to change it
        if not ok:
            log(f"claude quota: bootstrap request failed ({detail}) — claude stays unavailable")
            return self._claude_provider(readings, self._idle_notes(fp, readings))
        prov, readings = self._claude_pass(fp, tok)  # a FRESH usage response decides; no second attempt
        return prov

    def _claude_pass(self, fp: str | None, tok: str | None) -> tuple[Provider, list[Reading] | None]:
        """One read of the usage endpoint (cache-aware) turned into a Provider. Returns readings=None
        when no payload was obtained at all, so the caller can tell "the endpoint would not answer"
        from "the endpoint answered something we could not use"."""
        payload = self._cached_claude(fp)
        if payload is None:
            if not tok:
                return Provider("claude", False, None, error="no claude accessToken"), None
            headers = {"Authorization": f"Bearer {tok}", "anthropic-beta": CLAUDE_BETA, "User-Agent": "claude-code/2.1"}
            try:
                code, payload, retry_after = _http_get_json(CLAUDE_USAGE_URL, headers)
            except GitHubError as e:
                return Provider("claude", False, None, error=str(e)), None
            # The worker never refreshes: the operator owns the single-use refresh token (rotating it here
            # would invalidate their copy). An expired access token reads as unavailable until the operator's
            # external refresher rotates it and mirror_creds picks it up next cycle. (On macOS the keychain-
            # first read above already means we never hold a file refresh token to rotate.) Always name the
            # status code — an auth failure, a rate-limited endpoint and a server error are different
            # problems with different fixes, and none of them is "usage unknown".
            if code != 200 or not payload:
                err = f"claude usage HTTP {code}"
                if code == 401:
                    err += " (token expired; refresh left to the operator)"
                elif code == 429:
                    err += " (usage endpoint rate-limited)"
                elif code == 200:
                    err = "claude usage response empty"
                return Provider("claude", False, None, error=err, retry_after=retry_after), None
            readings = _claude_readings(payload)
            # Cache ONLY a payload that is fully resolved, and only until the first reset it describes.
            # An idle/absent/malformed payload is never pinned: re-reading it is the only thing that can
            # resolve it, and one extra fetch per poll is the cheapest part of this system.
            valid_until = _claude_valid_until(readings)
            if valid_until is not None:
                self._store_raw("claude", payload, fp, valid_until)
        else:
            readings = _claude_readings(payload)
        # A window that reads active again has been initialized — drop its bootstrap record so the NEXT
        # reset gets its own attempt.
        self._forget_bootstrap(fp, [r.window for r in readings if r.state == STATE_ACTIVE])
        return self._claude_provider(readings, self._idle_notes(fp, readings)), readings

    def _cached_claude(self, fp: str | None) -> dict | None:
        """A cached usage payload, or None when it must not be served. Validity is BOTH conditions:
        the payload is fully and safely interpretable, and the wall clock has not yet reached any reset
        it represents. `_cached_raw` enforces the stored expiry; re-deriving it here also covers an
        entry written before this rule existed (and any payload whose windows no longer parse)."""
        payload = self._cached_raw("claude", fp)
        if payload is None or _claude_valid_until(_claude_readings(payload)) is None:
            return None
        return payload

    def _claude_provider(self, readings: list[Reading], notes: dict[str, str] | None = None) -> Provider:
        """Both windows always gate: a window we cannot read blocks exactly as hard as one that is
        exhausted. `notes` supplies the bootstrap phase for idle windows."""
        notes = notes or {}
        wins = [_window_from_reading(r, notes.get(r.window)) for r in readings]
        err = None
        if wins and all(w.status == STATE_ABSENT for w in wins):
            # Not one recognizable record in the whole body: this is not a quota reading at all — the
            # schema moved, or we were handed something that isn't the usage response.
            err = "claude usage response schema unsupported (no session or weekly record)"
        avail = bool(wins) and all(w.status == "under-pace" for w in wins)
        return Provider("claude", avail, "opus" if avail else None, wins, err, self._next_eligible(wins))

    def _claude_from_payload(self, payload: dict) -> Provider:
        """Provider straight from a payload, with no cache, ledger or bootstrap — the pure parse."""
        return self._claude_provider(_claude_readings(payload))

    # --- the bounded post-reset bootstrap -----------------------------------
    def _idle_notes(self, fp: str | None, readings: list[Reading]) -> dict[str, str]:
        """What to say about each idle window, given what the ledger remembers. These are the states an
        operator needs to tell apart: nothing tried yet, a request that went through but whose window
        still isn't reporting, and a request that failed outright."""
        notes = {}
        for r in readings:
            if r.state != STATE_IDLE:
                continue
            rec = self._bootstrap_record(fp, r.window)
            if rec is None:
                notes[r.window] = _idle_phrase(r)
            elif rec.get("ok"):
                notes[r.window] = "bootstrap attempted; awaiting fresh usage"
            else:
                notes[r.window] = f"bootstrap failed: {rec.get('detail') or 'unknown error'}"
        return notes

    def _bootstrap_ledger(self, fp: str | None) -> dict:
        """The recorded bootstrap attempts for THIS account, minus any that have aged out. Keyed by the
        credential fingerprint so an account switch never inherits the other account's attempts."""
        d = _read_json_file(self.cache_dir / CLAUDE_BOOTSTRAP_FILE)
        if not isinstance(d, dict) or d.get("fp") != fp or not isinstance(d.get("windows"), dict):
            return {}
        now = time.time()
        return {
            w: rec
            for w, rec in d["windows"].items()
            if isinstance(rec, dict) and now - (rec.get("at") or 0) < CLAUDE_BOOTSTRAP_RETRY_S
        }

    def _bootstrap_record(self, fp: str | None, window: str) -> dict | None:
        return self._bootstrap_ledger(fp).get(window)

    def _remember_bootstrap(self, fp: str | None, windows: list[str], ok: bool, detail: str) -> None:
        led = self._bootstrap_ledger(fp)
        for w in windows:
            led[w] = {"at": int(time.time()), "ok": bool(ok), "detail": detail}
        self._write_bootstrap(fp, led)

    def _forget_bootstrap(self, fp: str | None, windows: list[str]) -> None:
        led = self._bootstrap_ledger(fp)
        if not any(w in led for w in windows):
            return  # steady state: no write at all
        for w in windows:
            led.pop(w, None)
        self._write_bootstrap(fp, led)

    def _write_bootstrap(self, fp: str | None, led: dict) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / CLAUDE_BOOTSTRAP_FILE).write_text(json.dumps({"fp": fp, "windows": led}))
        except OSError:
            pass  # best-effort; a ledger we can't write just means the next poll re-attempts

    def _claude_bootstrap_request(self) -> tuple[bool, str]:
        """ONE small `claude -p` turn, purely to open a window the endpoint reports as reset but not
        yet initialized. Deliberately the smallest real request we can make, not a round: a round is
        unbounded spend against a quota window we currently cannot read.

        It doubles as a safety check — if the window has NOT actually reset, this request is refused by
        the provider, we record the failure and stay blocked. So the bootstrap can never unlock
        spending by itself; it can only produce telemetry, and the fresh telemetry decides."""
        import shlex

        argv = [*(shlex.split(CLAUDE_CMD) or ["claude"]), "-p", CLAUDE_BOOTSTRAP_PROMPT]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # bill the subscription
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_BOOTSTRAP_TIMEOUT_S,
                cwd=str(self.cache_dir),  # a neutral cwd: no repo, no project config to drag in
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"{argv[0]} timed out after {CLAUDE_BOOTSTRAP_TIMEOUT_S}s"
        except FileNotFoundError:
            return False, f"{argv[0]} not found on PATH"
        except OSError as e:
            return False, f"could not run {argv[0]}: {e}"
        if p.returncode != 0:
            return False, f"{argv[0]} exited {p.returncode}{_tail_detail(p.stderr or p.stdout)}"
        return True, "ok"

    @staticmethod
    def _next_eligible(windows: list[Window]) -> float | None:
        # Earliest reset among windows that are currently blocking (over-pace/exhausted).
        blocked = [w.resets_at for w in windows if w.status in ("over-pace", "exhausted") and w.resets_at]
        return min(blocked) if blocked else None

    # --- selection ---------------------------------------------------------
    def choose(self, forced: str | None) -> tuple[str | None, dict]:
        """Return (agent_to_run_now or None, {codex: Provider, claude: Provider}).

        forced in {codex, claude}: only that provider counts. None/'auto': codex preferred, opus
        fallback. OpenRouter agents (deepseek/minimax) bypass this entirely (handled by the caller).
        """
        snap = {}
        if forced in (None, "auto", "codex"):
            snap["codex"] = self.codex()
        if forced in (None, "auto", "claude"):
            snap["claude"] = self.claude()
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
    ahead = [w for w in gating if w.status == "over-pace"]
    if ahead:
        bits = []
        for w in ahead:
            # Show the curve budget it exceeded (so a custom --pace is observable), plus quota remaining.
            vs = "" if (w.used is None or w.budget is None) else f" (used {round(w.used)}% > {round(w.budget)}% budget)"
            left = "" if w.used is None else f", {max(0, round(100 - w.used))}% left"
            bits.append(f"{w.name} ahead of pace{vs}{left}")
        return True, "; ".join(bits)
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
