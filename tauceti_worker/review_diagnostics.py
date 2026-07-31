"""Persistent local diagnostics and allow-listed public summaries for failed review commands."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from .runtime_status import atomic_json

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SECRET_RE = [
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[oprsu]_[A-Za-z0-9]{8,}|"
        r"github_pat_[A-Za-z0-9_]{8,}|xoxb-[A-Za-z0-9-]{8,})\b"
    ),
    re.compile(r"(?i)\b(?:authorization|bearer)\s*[:=]?\s+\S+"),
    re.compile(r"(?i)\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=\S+"),
    re.compile(r"https://[^/\s@]+@"),
]
_HOME_RE = re.compile(r"(?:(?:/home|/Users)/)[^/\s]+")
_PUBLIC_CATEGORIES = {
    "reviewer-auth",
    "missing-tool",
    "stale-head",
    "provider-unavailable",
    "checkout-or-network",
    "review-engine",
    "review-command",
}
_PUBLIC_PROVIDERS = {"claude", "codex", "deepseek", "minimax", "sonnet"}
_PUBLIC_DETAILS = {
    "reviewer-auth": "reviewer authentication failed",
    "missing-tool": "reviewer executable unavailable",
    "stale-head": "PR head changed before review",
    "provider-unavailable": "review provider unavailable",
    "checkout-or-network": "checkout or network operation failed",
    "review-engine": "review engine failed",
    "review-command": "review command failed",
}
_STAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def sanitize_failure(text: str, limit: int = 500) -> str:
    """Return a concise one-line diagnostic for local retention only.

    This best-effort cleanup makes local state easier to inspect, but it is deliberately not a
    publication boundary: arbitrary subprocess output can contain unanticipated credential forms.
    `public_review_failure` publishes only fixed allow-listed facts and never this returned text.
    """
    clean = _ANSI_RE.sub("", str(text or ""))
    clean = _CONTROL_RE.sub(" ", clean)
    for pattern in _SECRET_RE:
        clean = pattern.sub("[REDACTED]", clean)
    clean = _HOME_RE.sub("/[home]", clean)
    clean = " ".join(clean.split())
    return clean[-limit:]


def classify_failure(summary: str) -> str:
    """Coarse failure class used for alerts and future retry policy."""
    low = summary.lower()
    if any(s in low for s in ("not logged in", "run /login", "authentication", "credential")):
        return "reviewer-auth"
    if any(s in low for s in ("not found on path", "no such file or directory", "command not found")):
        return "missing-tool"
    if "expected" in low and "head" in low:
        return "stale-head"
    if any(s in low for s in ("rate limit", "too many requests", "429", "overloaded", "529")):
        return "provider-unavailable"
    if any(s in low for s in ("git clone", "git fetch", "could not resolve host", "connection reset")):
        return "checkout-or-network"
    if any(s in low for s in ("traceback", "exception", "error:")):
        return "review-engine"
    return "review-command"


def _last_log_line(log_file: Path | None) -> str:
    if log_file is None:
        return ""
    try:
        lines = log_file.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return next((line.strip() for line in reversed(lines) if line.strip()), "")


def _path(state: Path, pr: int) -> Path:
    return state / f"review-failure-{pr}.json"


def read_review_failure(state: Path, pr: int) -> dict:
    try:
        value = json.loads(_path(state, pr).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def recover_review_failures(state: Path, log_dir: Path, *, worker: str, pr: int, head: str = "") -> dict:
    """Backfill diagnostics from review logs written before structured retention was deployed."""
    existing = read_review_failure(state, pr)
    if existing:
        return existing
    try:
        logs = sorted(log_dir.glob(f"review-{pr}-*.log"), key=lambda path: path.stat().st_mtime)[-3:]
    except OSError:
        return {}
    attempts = []
    for log_file in logs:
        summary = sanitize_failure(_last_log_line(log_file))
        if not summary:
            continue
        try:
            stamp = datetime.datetime.fromtimestamp(log_file.stat().st_mtime, datetime.UTC)
        except OSError:
            stamp = datetime.datetime.now(datetime.UTC)
        attempts.append(
            {
                "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "worker": worker,
                "head": head,
                "provider": "unknown",
                "code": None,
                "category": classify_failure(summary),
                "summary": summary,
                "log": log_file.name,
                "recovered": True,
            }
        )
    if not attempts:
        return {}
    value = {"schema": "tauceti.review-failure/v1", "pr": pr, "attempts": attempts}
    atomic_json(_path(state, pr), value)
    return value


def record_review_failure(
    state: Path,
    *,
    worker: str,
    pr: int,
    head: str,
    provider: str,
    code: int,
    reason: str = "",
    log_file: Path | None = None,
) -> dict:
    """Append one failure, retaining the latest three attempts for this PR and worker."""
    summary = sanitize_failure(reason)
    if not summary or summary == f"review #{pr} exited with status {code}":
        summary = sanitize_failure(_last_log_line(log_file))
    if not summary:
        summary = f"review command exited with status {code}"
    attempt = {
        "at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worker": worker,
        "head": head,
        "provider": provider,
        "code": int(code),
        "category": classify_failure(summary),
        "summary": summary,
        "log": log_file.name if log_file is not None else None,
    }
    previous = read_review_failure(state, pr)
    attempts = previous.get("attempts") if isinstance(previous.get("attempts"), list) else []
    value = {"schema": "tauceti.review-failure/v1", "pr": pr, "attempts": [*attempts, attempt][-3:]}
    atomic_json(_path(state, pr), value)
    return value


def clear_review_failure(state: Path, pr: int) -> None:
    _path(state, pr).unlink(missing_ok=True)


def public_review_failure(value: dict) -> str:
    """Compact public account built only from fixed labels; never publish subprocess text."""
    attempts = value.get("attempts") if isinstance(value.get("attempts"), list) else []
    rows = []
    for attempt in attempts[-3:]:
        if not isinstance(attempt, dict):
            continue
        category = attempt.get("category")
        category = category if category in _PUBLIC_CATEGORIES else "review-command"
        provider = attempt.get("provider")
        provider = provider if provider in _PUBLIC_PROVIDERS else "unknown"
        stamp = attempt.get("at")
        stamp = stamp if isinstance(stamp, str) and _STAMP_RE.fullmatch(stamp) else "unknown time"
        code = attempt.get("code")
        exit_note = f"exit {code}" if isinstance(code, int) else "exit unknown"
        summary = str(attempt.get("summary", "")).lower()
        detail = (
            "review prompt exceeded the OS argument limit"
            if "argument list too long" in summary or "e2big" in summary
            else _PUBLIC_DETAILS[category]
        )
        rows.append(f"- {stamp}: `{category}` via `{provider}` ({exit_note}): {detail}")
    return "\n".join(rows)
