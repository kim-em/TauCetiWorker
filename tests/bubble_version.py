#!/usr/bin/env python3
"""Bubble rounds must use the stable 0.7.28+ SSH-target fix instead of starting a doomed round."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


for version, accepted in (
    ("0.7.27", False),
    ("0.7.28", True),
    ("0.7.28+local", True),
    ("0.8.0", True),
    ("", False),
    ("not-a-version", False),
):
    check(
        f"version comparison: {version or '<empty>'}",
        tc.bubble_version_meets_minimum(version) is accepted,
    )


def opts(*, dry_run=False):
    return tc.RoundOpts(
        only=["review"],
        agent="claude",
        work_model="claude",
        sandbox_host=False,
        dry_run=dry_run,
    )


saved_have = tc.cli._have
saved_disposable = tc.cli.bubble_cmd_is_disposable
saved_version = tc.cli.installed_bubble_version
try:
    tc.cli._have = lambda _tool: True
    tc.cli.bubble_cmd_is_disposable = lambda: False

    tc.cli.installed_bubble_version = lambda: "0.7.27"
    try:
        tc.cli.preflight(SimpleNamespace(), opts())
        blocked = False
    except tc.Die as exc:
        blocked = "0.7.28" in str(exc) and "--force" in str(exc)
    check("real bubble round rejects 0.7.27 with update guidance", blocked)

    tc.cli.installed_bubble_version = lambda: "0.7.28"
    try:
        tc.cli.preflight(SimpleNamespace(), opts())
        accepted = True
    except tc.Die:
        accepted = False
    check("real bubble review accepts 0.7.28", accepted)

    tc.cli.installed_bubble_version = lambda: "0.7.27"
    try:
        tc.cli.preflight(SimpleNamespace(), opts(dry_run=True))
        dry_run_accepted = True
    except tc.Die:
        dry_run_accepted = False
    check("dry-run remains side-effect-free and skips the installed-version gate", dry_run_accepted)
finally:
    tc.cli._have = saved_have
    tc.cli.bubble_cmd_is_disposable = saved_disposable
    tc.cli.installed_bubble_version = saved_version

sys.exit(1 if fails else 0)
