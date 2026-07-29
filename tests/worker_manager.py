#!/usr/bin/env python3
"""Integration checks for declarative configuration and portable worker reconciliation."""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

root = Path(tempfile.mkdtemp(prefix="tauceti-workers-test-"))
for key in (
    "TAUCETI_CONFIG_HOME",
    "TAUCETI_WORKERS_CONFIG",
    "TAUCETI_WORKERS_STATE_DIR",
    "TAUCETI_RUNTIME_DIR",
):
    os.environ.pop(key, None)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
os.environ["TAUCETI_RUNTIME_DIR"] = str(root / "run")
os.environ["TAUCETI_MANAGER_TEST_COMMAND"] = shlex.join(
    [sys.executable, "-c", "import os; os.read(int(os.environ['TAUCETI_PARENT_PIPE_FD']), 1)"]
)

import tauceti_worker.worker_manager as wm
from tauceti_worker.cli import build_parser
from tauceti_worker.runtime_status import report_runtime


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def start_manager(config):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tauceti_worker",
            "workers",
            "--config",
            str(config),
            "manager",
            "--interval",
            "0.05",
        ],
        cwd=REPO,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


config = wm.default_workers_config()
assert config.is_relative_to(root), f"test configuration escaped its temporary root: {config}"
specs = [
    wm.WorkerSpec(id="worker1"),
    wm.WorkerSpec(id="worker2", agent="codex", only=("review",)),
    wm.WorkerSpec(id="worker3", only=("roadmap",), roadmap_only="RepresentationTheory"),
]
manager = None
try:
    wm.save_worker_specs(config, specs)
    assert wm.load_worker_specs(config) == specs

    runtime_probe = root / "runtime-probe.json"
    os.environ["TAUCETI_RUNTIME_STATUS"] = str(runtime_probe)
    report_runtime("waiting-quota", detail="test", next_action_at=123)
    assert wm.read_json(runtime_probe)["state"] == "waiting-quota"
    os.environ.pop("TAUCETI_RUNTIME_STATUS")

    # Legacy command import produces the same semantic settings without retaining shell syntax.
    legacy = root / "workers.conf"
    legacy.write_text("./tauceti work --loop --worker-id worker2 --agent codex --only rebase,review --ignore-quota\n")
    imported = wm.parse_legacy_config(legacy)
    assert imported[0].id == "worker2"
    assert imported[0].only == ("rebase", "review")
    assert imported[0].ignore_quota is True

    # The full semantic model must remain parseable by the real work CLI.
    maximal = wm.WorkerSpec(
        id="maximal",
        agent="codex",
        only=("roadmap", "review"),
        sandbox="bubble",
        ignore_quota=True,
        roadmap_only="RepresentationTheory",
        roadmap_skip=("Algebra",),
        roadmap_extra_identities=("Maintainer",),
        respect_claims=False,
        source="https://example.invalid/source",
        author_model="gpt-5",
        author_effort="high",
        pace="50%@1h",
        stream=True,
        isolate_home=True,
    )
    parsed = build_parser().parse_args(maximal.work_argv()[3:])
    assert parsed.cmd == "work" and parsed.worker_id == "maximal"

    # Desired fields win over a stale actual status generation in CLI/TUI snapshots.
    wm.update_status(
        wm.status_path("snapshot"),
        managed=True,
        agent="codex",
        only=["review"],
        sandbox="bubble",
        spec_hash="old",
    )
    desired_snapshot = wm.worker_snapshots([wm.WorkerSpec(id="snapshot", agent="claude")])[0]
    assert desired_snapshot["agent"] == "claude"
    assert desired_snapshot["actual_spec_hash"] == "old"

    if sys.platform != "darwin":
        assert wm._service_path() == root / "config" / "systemd" / "user" / "tauceti-workers.service"
        unit = wm._systemd_unit(config)
        assert 'Environment="PATH=' in unit and 'Environment="PYTHONPATH=' in unit
        assert "ExecStart=" in unit and str(config.resolve()) in unit

    manager = start_manager(config)
    wait_for(lambda: wm.manager_request("ping"))

    def healthy_rows():
        rows = wm.worker_snapshots(specs)
        return rows if len(rows) == 3 and all(row.get("alive") for row in rows) else None

    first = wait_for(healthy_rows)
    pids = {row["id"]: row["wrapper_pid"] for row in first}

    # A hard-killed wrapper cannot strand its child: pipe EOF stops it and the manager repairs the slot.
    worker3 = wm.runner_status("worker3")
    old_worker3_child = worker3["child_pid"]
    os.kill(worker3["wrapper_pid"], 9)
    wait_for(lambda: wm.runner_status("worker3").get("wrapper_pid") not in (None, pids["worker3"]))

    def old_child_gone():
        try:
            os.kill(old_worker3_child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(old_child_gone)

    # An explicit restart replaces exactly one managed wrapper.
    assert wm.manager_request("restart", id="worker2")
    wait_for(lambda: wm.runner_status("worker2").get("wrapper_pid") not in (None, pids["worker2"]))
    assert wm.runner_status("worker1").get("wrapper_pid") == pids["worker1"]

    # Disabling is persistent desired state and stops that worker without disturbing peers.
    wm.set_worker_enabled(config, "worker3", False)
    wait_for(lambda: not wm.runner_status("worker3").get("alive"))
    assert wm.load_worker_specs(config)[2].enabled is False
    assert wm.runner_status("worker1").get("wrapper_pid") == pids["worker1"]

    # A changed definition restarts only the changed worker.
    current = wm.load_worker_specs(config)
    wm.save_worker_specs(
        config,
        [dataclasses.replace(spec, agent="claude") if spec.id == "worker1" else spec for spec in current],
    )
    assert wm.manager_request("apply")
    wait_for(lambda: wm.runner_status("worker1").get("wrapper_pid") not in (None, pids["worker1"]))

    # A malformed generation is rejected wholesale; the last good fleet remains alive.
    worker1_pid = wm.runner_status("worker1").get("wrapper_pid")
    config.write_text("version = 1\n[[workers]]\nid = 42\n")
    time.sleep(0.3)
    assert wm.runner_status("worker1").get("wrapper_pid") == worker1_pid
    response = wm.manager_request("ping")
    assert response and response.get("error")

    # Even after a manager restart, an initially malformed file must not stop surviving workers.
    survivors = {wid: wm.runner_status(wid).get("wrapper_pid") for wid in ("worker1", "worker2")}
    assert wm.manager_request("shutdown", stop_workers=False)
    manager.wait(10)
    manager = start_manager(config)
    wait_for(lambda: (reply := wm.manager_request("ping")) and reply.get("error"))
    assert {wid: wm.runner_status(wid).get("wrapper_pid") for wid in survivors} == survivors

    # Shutdown against an initially malformed config stops surviving wrappers without iterating None.
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    assert manager.returncode == 0
    manager = None
    wait_for(lambda: not any(wm.runner_status(wid).get("alive") for wid in survivors))

    # Explicit restart revives a terminal restart="never" worker without changing its policy.
    never = wm.WorkerSpec(id="never", restart="never")
    wm.save_worker_specs(config, [never])
    manager = start_manager(config)
    wait_for(lambda: wm.runner_status("never").get("alive"))
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    manager = start_manager(config)
    wait_for(lambda: wm.manager_request("ping"))
    time.sleep(0.3)
    assert not wm.runner_status("never").get("alive")
    wm.restart_worker(config, "never")
    wait_for(lambda: wm.runner_status("never").get("alive"))

    # Manager shutdown can stop the complete fleet, leaving readable terminal status records.
    assert wm.manager_request("shutdown", stop_workers=True)
    manager.wait(15)
    assert manager.returncode == 0
    manager = None
    wait_for(lambda: not wm.runner_status("never").get("alive"))
    print("worker manager: OK")
finally:
    if manager is not None and manager.poll() is None:
        wm.manager_request("shutdown", stop_workers=True)
        try:
            manager.wait(10)
        except subprocess.TimeoutExpired:
            manager.terminate()
            manager.wait(5)
    for state_file in (root / "state" / "tauceti" / "workers").glob("*.json"):
        wm._stop_runner(state_file.stem)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ids = [path.stem for path in (root / "state" / "tauceti" / "workers").glob("*.json")]
        if not any(wm.runner_status(wid).get("alive") for wid in ids):
            break
        time.sleep(0.05)
    shutil.rmtree(root, ignore_errors=True)
