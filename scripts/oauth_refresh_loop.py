#!/usr/bin/env python3
"""Single-writer OAuth refresher for an unattended TauCeti Docker worker.

The rotation itself lives in `tauceti_worker.oauth`, shared with the pacer's own auto-refresh so
there is exactly one implementation of single-use-token handling. This file is the daemon around it:
the poll loop, the env-var knobs, and the back-off on a persistent failure.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

try:
    from tauceti_worker import oauth
except ModuleNotFoundError:  # installed standalone (Docker) — the checkout is the script's grandparent
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tauceti_worker import oauth


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from error
    if value < 1:
        raise ValueError(f"{name} must be positive (got {value})")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("claude", "codex"))
    args = parser.parse_args()

    provider = oauth.provider(args.provider)
    try:
        poll_seconds = _positive_env("TAUCETI_REFRESH_POLL_SECONDS", 60)
        skew_seconds = _positive_env("TAUCETI_REFRESH_SKEW_SECONDS", 5400)
        minimum_interval = _positive_env("TAUCETI_REFRESH_MIN_INTERVAL_SECONDS", 600)
        maximum_backoff = _positive_env("TAUCETI_REFRESH_MAX_BACKOFF_SECONDS", 900)
    except ValueError as error:
        parser.error(str(error))
    run_once = os.environ.get("TAUCETI_REFRESH_ONCE") == "1"
    stop = threading.Event()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda _signum, _frame: stop.set())

    print(f"{provider.name}-refresh: monitoring {provider.credentials}", flush=True)
    backoff = poll_seconds
    while not stop.is_set():
        try:
            result = oauth.refresh_if_due(provider, skew_seconds, minimum_interval_seconds=minimum_interval)
            if result == "waiting":
                print(f"{provider.name}-refresh: waiting for {provider.credentials}", flush=True)
            elif result == "refreshed":
                print(f"{provider.name}-refresh: credential refreshed", flush=True)
            backoff = poll_seconds
        except (OSError, RuntimeError, ValueError) as error:
            print(f"{provider.name}-refresh: {error}; retrying in {backoff}s", file=sys.stderr, flush=True)
            if run_once:
                return 1
            if stop.wait(backoff):
                break
            backoff = min(maximum_backoff, max(poll_seconds, backoff * 2))
            continue
        if run_once:
            return 0
        stop.wait(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
