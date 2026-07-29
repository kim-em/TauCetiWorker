"""Atomic structured status shared by managed worker processes and the local dashboard."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

STATUS_ENV = "TAUCETI_RUNTIME_STATUS"
_RICH_STYLE_RE = re.compile(r"\[(?:/?(?:bold|red|yellow|green|dim)(?: [^]]+)?|/)\]")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def update_status(path: Path, **changes) -> dict:
    """Merge changes into a status file under a sibling flock and replace it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read_json(path)
        value.update(changes)
        value["status_updated_at"] = time.time()
        atomic_json(path, value)
        return value


def report_runtime(state: str | None = None, **changes) -> None:
    """Best-effort status update from a worker or round child.

    Unmanaged workers have no ``TAUCETI_RUNTIME_STATUS`` and pay only the environment lookup.
    Status reporting must never turn useful work into a failed round.
    """
    raw = os.environ.get(STATUS_ENV)
    if not raw:
        return
    if state is not None:
        changes["state"] = state
    if isinstance(changes.get("detail"), str):
        changes["detail"] = _RICH_STYLE_RE.sub("", changes["detail"])
    changes["activity_at"] = time.time()
    try:
        update_status(Path(raw), **changes)
    except Exception:
        pass
