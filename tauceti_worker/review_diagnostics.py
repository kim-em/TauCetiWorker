"""Persistent, public-safe diagnostics for review commands that exit before posting a verdict."""

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


def sanitize_failure(text: str, limit: int = 500) -> str:
    """Return a one-line diagnostic safe to copy into a public GitHub issue.

    Reviewer logs stay local. This keeps only one concise summary after removing common credential
    shapes, credential-bearing URLs, user-specific home paths, terminal escapes, and control bytes.
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
    """Compact Markdown-safe account of the retained attempts, with no local paths or raw logs."""
    attempts = value.get("attempts") if isinstance(value.get("attempts"), list) else []
    rows = []
    for attempt in attempts[-3:]:
        if not isinstance(attempt, dict):
            continue
        summary = sanitize_failure(attempt.get("summary", "")).replace("`", "'").replace("<", "&lt;")
        rows.append(
            f"- {attempt.get('at', 'unknown time')}: `{attempt.get('category', 'review-command')}` "
            f"via `{attempt.get('provider', 'unknown')}` (exit {attempt.get('code', '?')}): {summary}"
        )
    return "\n".join(rows)
