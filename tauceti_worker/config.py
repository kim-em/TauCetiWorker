"""tauceti_worker.config — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import fcntl
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import ROADMAP
from .paths import HERE


def roadmap_focus() -> str | None:
    """The roadmap area the worker steers toward, as the operator set it — read live from the env
    each call so the TUI's [f] key can change it and have both the survey display and any launched
    round pick it up (children inherit TAUCETI_ROADMAP_FOCUS). Tri-state: None = unset, so a fresh
    random area is picked per round (see do_roadmap); "" = all areas; else the area name. There is
    deliberately no baked-in default, and nothing here consults the dashboard prefs — a bare CLI run
    resolves only from the env + the live area list, never from a saved dashboard preference."""
    return os.environ.get("TAUCETI_ROADMAP_FOCUS")


def _focus_label(sv=None) -> str:
    """Friendly render of the roadmap focus for the status bar. Prefers the survey's sanitized value
    ("auto"/"any"/area); falls back to the raw env tri-state before the first survey lands."""
    v = sv.roadmap_focus if (sv is not None and sv.roadmap_focus) else roadmap_focus()
    if v is None or v == "auto":
        return "auto (random each round)"
    return "all areas" if v in ("", "any") else v


def roadmap_areas(gh) -> list[str]:
    """The roadmap focus areas a user can steer toward: the subdirectories of the roadmap repo (each
    holds a README.md + Targets.lean). Listed over the API so the TUI can offer a picker. Returns []
    if it can't be fetched (the picker then falls back to free-text entry)."""
    out = gh.api_jq(f"repos/{ROADMAP}/contents/TauCetiRoadmap", '.[] | select(.type=="dir") | .name')
    return sorted(out.splitlines()) if out else []


def sanitize_wid(raw: str) -> str:
    s = raw.lower()
    return re.sub(r"[^a-z0-9-]", "-", s)


# Slots held by the current process for its whole life. The fds must NOT be closed (closing releases the
# flock and frees the slot); keeping the ints here also documents the intentional hold and prevents any
# tooling from flagging them as leaks.
_HELD_SLOTS: list[int] = []


def acquire_slot(wid: str) -> bool:
    """Hold an instance lock on this worker slot for the life of the process, returning False if another
    process already holds it. The lock auto-releases on exit/crash (the kernel drops the flock), so a
    restart reclaims the slot — reusing its seeded $HOME and recognising its own leases. Mirrors
    RoundContext's flock-NB pattern, one level up (per process, not per round)."""
    state = HERE / "state" / wid
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "instance.lock", os.O_CREAT | os.O_WRONLY, 0o644)
    os.set_inheritable(fd, False)  # spawned rounds take their own round.lock; don't inherit this one
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _HELD_SLOTS.append(fd)
    return True


def auto_assign_wid(limit: int = 64) -> str:
    """Pick the lowest-numbered free worker slot (worker1, worker2, ...) and hold it, so several
    `work --loop` terminals on one host get distinct ids without the operator hand-numbering them."""
    for n in range(1, limit + 1):
        wid = f"worker{n}"
        if acquire_slot(wid):
            return wid
    raise Die(f"all {limit} worker slots are busy — pass an explicit --worker-id")


@dataclass(frozen=True)
class Config:
    """Resolved per-worker configuration (paths derived from the worker id)."""

    wid: str
    home: Path  # HOME the worker runs under (may be isolated)
    state: Path  # HERE/state/<wid>
    checkout: Path  # host authoring checkout
    store_dir: Path  # tauceti-review persistent store
    sbcache: Path  # scoreboard meta cache dir
    logdir: Path  # HERE/logs/<wid>
    quota_cache: Path  # raw provider usage responses

    @property
    def store(self) -> Path:
        return self.store_dir / "ledger.json"

    @staticmethod
    def resolve(worker_id: str | None = None, home: Path | None = None) -> Config:
        wid = sanitize_wid(worker_id or os.environ.get("TAUCETI_WORKER_ID", "default") or "default")
        # Export the resolved id so claim.sh (acquire / heartbeat-renew / git-safe-push's lease check)
        # all share ONE stable owner identity. Without this it falls back to `hostname-$$`, a different
        # owner per claim.sh invocation, so on the --host path a worker can't renew or recognise its own
        # branch/<pr> lease and git-safe-push fails closed with "lease lost (another agent took over)".
        os.environ["TAUCETI_WORKER_ID"] = wid
        h = home or Path(os.environ.get("HOME", os.path.expanduser("~")))
        state = HERE / "state" / wid
        return Config(
            wid=wid,
            home=h,
            state=state,
            checkout=HERE / "checkouts" / wid / "TauCeti",
            store_dir=h / ".cache" / "tauceti-review" / wid / "store" / "FormalFrontier__TauCeti",
            sbcache=state / "cache" / "scoreboard",
            logdir=HERE / "logs" / wid,
            quota_cache=state / "cache",
        )


_LOG_FH = None  # set by set_log_file(): tee log() to a per-worker file

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")  # strip color codes from the on-disk copy


def set_log_file(logdir: Path) -> None:
    """Tee log() output to a per-worker file so a worker's activity is readable ON DISK, not only on the
    terminal it was launched from (a loop run directly in a shell otherwise leaves no log to inspect).
    The loop driver picks ONE timestamped file and exports its path so every round child appends to the
    SAME file — one continuous session log per worker; a standalone round with no parent makes its own.
    Idempotent and best-effort: a failure to open the file just leaves logging at stderr-only."""
    global _LOG_FH
    if _LOG_FH is not None:
        return
    path = os.environ.get("TAUCETI_LOG_FILE")
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        if not path:
            path = str(logdir / f"work-{time.strftime('%Y%m%d-%H%M%S')}.log")
            os.environ["TAUCETI_LOG_FILE"] = path  # children inherit and append to the same file
        _LOG_FH = open(path, "a", buffering=1)  # line-buffered: each log line flushes on its newline
    except OSError:
        _LOG_FH = None


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} tauceti: {msg}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(_ANSI_RE.sub("", line) + "\n")
        except (OSError, ValueError):
            pass


_RED, _RESET = "\033[1;31m", "\033[0m"


def warn_red(msg: str) -> None:
    """A bright-red, attention-demanding log line (a PR the automation can't make progress on)."""
    log(f"{_RED}⚠ {msg}{_RESET}")


class Die(Exception):
    """Fatal error → exit 1."""


class NoProgress(Exception):
    """Round did no productive work → exit EX_NOPROGRESS (75)."""
