"""tauceti_worker.quota — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, log
from .github import GitHubError, _parse_retry_after

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CLAUDE_BETA = "oauth-2025-04-20"

SESSION_WINDOW_S = 5 * 3600

WEEK_WINDOW_S = 7 * 24 * 3600

QUOTA_TTL = {"codex": 600, "claude": 3600}


@dataclass
class Window:
    name: str
    used: float | None  # percent 0..100 (None = unknown)
    elapsed: float | None  # percent 0..100 (None = unknown)
    resets_at: float | None  # epoch seconds
    status: str  # under-pace | over-pace | exhausted | unknown


@dataclass
class Provider:
    name: str  # codex | claude
    available: bool  # all windows under pace (and a usable model)
    model: str | None  # gpt-5 | opus | sonnet | None
    windows: list[Window] = field(default_factory=list)
    error: str | None = None
    next_eligible: float | None = None  # epoch when a blocking window should free
    retry_after: float | None = None  # seconds the endpoint asked us to back off (HTTP 429); NOT a

    # quota reset — must not be classified as exhausted/next_eligible


def _classify_window(
    name: str, used: float | None, elapsed: float | None, resets_at: float | None, limit_reached: bool
) -> Window:
    # Fail CLOSED on missing data. These endpoints are reverse-engineered: a schema drift that drops
    # `used` or the reset clock must read as 'unknown' (⇒ provider unavailable), NEVER as fresh /
    # under-pace, so it can't silently unlock spending. Clamp both percentages to [0,100].
    e = None if elapsed is None else max(0.0, min(100.0, elapsed))
    if limit_reached:
        return Window(name, used, e, resets_at, "exhausted")
    if used is None or e is None:
        return Window(name, used, e, resets_at, "unknown")
    u = max(0.0, min(100.0, used))
    st = "exhausted" if u >= 100 else ("under-pace" if u <= e else "over-pace")
    return Window(name, used, e, resets_at, st)


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
        return d.get("payload")

    def _store_raw(self, provider: str, payload: dict, fp: str | None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"quota-{provider}.json").write_text(
            json.dumps({"fetched_at": int(time.time()), "fp": fp, "payload": payload})
        )

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
        for key, name in (("primary_window", "session"), ("secondary_window", "weekly")):
            w = rl.get(key) or {}
            lim = w.get("limit_window_seconds")
            ra = w.get("reset_after_seconds")
            elapsed = None
            resets = None
            if isinstance(lim, (int, float)) and lim > 0 and isinstance(ra, (int, float)):
                elapsed = (lim - ra) / lim * 100
                resets = time.time() + max(0, ra)
            wins.append(_classify_window(name, w.get("used_percent"), elapsed, resets, limit_reached))
        avail = all(x.status == "under-pace" for x in wins) and not limit_reached
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
        mirror_creds(self.cfg)  # re-sync the isolated copy from the operator's fresh file
        oauth, from_keychain = self._claude_creds()
        if not oauth:
            err = f"no {claude_dir(self.cfg.home) / '.credentials.json'}"
            if sys.platform == "darwin":
                err += ' (and no "Claude Code-credentials" Keychain entry)'
            return Provider("claude", False, None, error=err)
        # No stable account id is exposed in the oauth block, so fingerprint the access token: a rotation to
        # a different account changes it (forcing a re-fetch); an external same-account refresh also changes
        # it, which only costs one harmless extra fetch every several hours.
        fp = self._fingerprint(oauth.get("accessToken"))
        payload = self._cached_raw("claude", fp)
        if payload is None:
            tok = oauth.get("accessToken")
            if not tok:
                return Provider("claude", False, None, error="no claude accessToken")
            headers = {"Authorization": f"Bearer {tok}", "anthropic-beta": CLAUDE_BETA, "User-Agent": "claude-code/2.1"}
            try:
                code, payload, retry_after = _http_get_json(CLAUDE_USAGE_URL, headers)
            except GitHubError as e:
                return Provider("claude", False, None, error=str(e))
            # The worker never refreshes: the operator owns the single-use refresh token (rotating it here
            # would invalidate their copy). An expired access token reads as unavailable until the operator's
            # external refresher rotates it and mirror_creds picks it up next cycle. (On macOS the keychain-
            # first read above already means we never hold a file refresh token to rotate.)
            if code != 200 or not payload:
                err = (
                    "claude token expired; refresh left to the operator" if code == 401 else f"claude usage HTTP {code}"
                )
                return Provider("claude", False, None, error=err, retry_after=retry_after)
            self._store_raw("claude", payload, fp)
        return self._claude_from_payload(payload)

    def _claude_from_payload(self, payload: dict) -> Provider:
        def win(key, name, window_s):
            w = payload.get(key) or {}
            used = w.get("utilization")
            resets = self._parse_iso(w.get("resets_at"))
            elapsed = None
            if resets is not None:
                remaining = resets - time.time()
                elapsed = (window_s - remaining) / window_s * 100
            return _classify_window(name, used, elapsed, resets, False)

        session = win("five_hour", "session", SESSION_WINDOW_S)
        weekly = win("seven_day", "weekly", WEEK_WINDOW_S)
        sonnet = win("seven_day_sonnet", "weekly_sonnet", WEEK_WINDOW_S)
        # All-null across windows ⇒ API unreachable/auth broken ⇒ unavailable.
        if all(x.used is None for x in (session, weekly, sonnet)):
            return Provider("claude", False, None, [session, weekly, sonnet], error="all usage null")
        opus_ok = session.status == "under-pace" and weekly.status == "under-pace"
        sonnet_ok = sonnet.status == "under-pace"
        model = "opus" if opus_ok else ("sonnet" if sonnet_ok else None)
        # The worker wants opus; sonnet does NOT count as available for work.
        avail = opus_ok
        nxt = self._next_eligible([session, weekly])
        return Provider("claude", avail, model, [session, weekly, sonnet], None, nxt)

    @staticmethod
    def _parse_iso(s: str | None) -> float | None:
        if not s:
            return None
        try:
            from datetime import datetime

            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

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


def _unavail_reason(prov: Provider) -> tuple[bool, str]:
    """Why an unavailable provider can't be used, and whether the block is *soft*.

    A soft block means there is real quota left and we're only pausing to pace the burn (over-pace) —
    distinct from a hard block where a window is exhausted or its usage is unknown (fail-closed). The
    `weekly_sonnet` window never gates opus, so it is ignored here. Returns (soft, reason)."""
    gating = [w for w in (prov.windows or []) if w.name != "weekly_sonnet"]
    spent = [w for w in gating if w.status == "exhausted"]
    if spent:
        return False, ", ".join(f"{w.name} exhausted" for w in spent)
    ahead = [w for w in gating if w.status == "over-pace"]
    if ahead:
        bits = []
        for w in ahead:
            left = "" if w.used is None else f", {max(0, round(100 - w.used))}% left"
            bits.append(f"{w.name} ahead of pace{left}")
        return True, "; ".join(bits)
    if any(w.status == "unknown" for w in gating):
        return False, "usage unknown"
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
