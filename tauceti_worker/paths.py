"""tauceti_worker.paths — install-location detection and self-invocation helpers.

`HERE` is the single source of truth for where the worker's bundled assets (prompts/, scripts/)
and runtime dirs (state/, checkouts/, logs/) live. It must resolve to the SAME directory the
old single-file `tauceti` used: the repo root in a source checkout, and the package dir in an
installed wheel. We detect which by asking whether `prompts/` sits beside the modules — only true
in the wheel (where pyproject force-includes it into the package); in a checkout prompts/ stays at
the repo root, one level up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_pkg = Path(__file__).resolve().parent  # …/tauceti_worker
HERE = _pkg if (_pkg / "prompts").is_dir() else _pkg.parent

# The branch-lease helper the agents run on PATH inside a round. Overridable for tests.
CLAIM_SH = os.environ.get("TAUCETI_CLAIM_SH") or str(HERE / "scripts" / "claim.sh")


def self_argv(*tail) -> list[str]:
    """argv to re-invoke this worker as a child (a `_round` or `_heartbeat` subprocess). `-m`
    works from a source checkout (paired with self_env's PYTHONPATH) and from an installed wheel
    alike — unlike the old `[sys.executable, __file__]`, which pointed at a now-unrunnable module."""
    return [sys.executable, "-m", "tauceti_worker", *(str(a) for a in tail)]


def self_env(env: dict | None = None) -> dict:
    """Environment for a self_argv child: ensure the package is importable. In a source checkout
    HERE is the repo root (which contains tauceti_worker/), so putting it on PYTHONPATH lets the
    child `python -m tauceti_worker`; in a wheel the package is already importable and this is a
    harmless no-op."""
    base = dict(os.environ if env is None else env)
    existing = base.get("PYTHONPATH")
    base["PYTHONPATH"] = os.pathsep.join([str(HERE)] + ([existing] if existing else []))
    return base


def entry_cmd() -> list[str]:
    """The user-facing command that runs this worker, for the TUI's copy/launch helpers. A source
    checkout has the executable `./tauceti` shim beside HERE; an installed wheel puts `tauceti` on
    PATH as a console script."""
    shim = HERE / "tauceti"
    return [str(shim)] if shim.exists() else ["tauceti"]
