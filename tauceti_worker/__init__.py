"""tauceti_worker — the Tau Ceti worker.

Bare `tauceti` opens a dashboard + launcher; `tauceti work [--loop]` does the work (one round, or
the driver loop); `tauceti status` prints the read-only survey.

The worker acts on FormalFrontier/TauCeti as the authenticated `gh` account, and treats that
account's own PRs as the ones it tends. Each round does exactly ONE unit of work, chosen in
priority order: rebase -> review -> fix-ci -> fix -> bump -> roadmap.

This package was split from a single-file script for navigability. The split is behaviour-
preserving. For both the test harness (which reaches `tauceti_worker.<NAME>`) and the historical
single-module API, every submodule's top-level name is flattened into the package namespace below;
the submodules themselves stay importable (e.g. `tauceti_worker.github`) so a test can monkeypatch
a function on the module that actually looks it up.
"""

from __future__ import annotations

# stdlib re-exports for the historical single-module surface (a few tests read tc.json, tc.time, …).
import json  # noqa: F401
import random  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401

from . import (
    agents,
    cli,
    config,
    constants,
    github,
    loop,
    paths,
    quota,
    review_state,
    round,
    survey,
    tui,
    work_units,
)

# Flatten each submodule's public + private top-level names into the package namespace, in
# dependency order, so the entire former single-module surface is reachable as tauceti_worker.<NAME>.
_MODULES = (paths, constants, config, github, quota, review_state, survey, round, agents, work_units, loop, tui, cli)
for _m in _MODULES:
    for _k, _v in vars(_m).items():
        if not _k.startswith("__"):
            globals()[_k] = _v
del _m, _k, _v
