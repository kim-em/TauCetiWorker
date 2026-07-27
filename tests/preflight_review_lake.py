#!/usr/bin/env python3
"""Host authoring must preflight Lake in the environment and login shell the agent actually receives.

The parent worker can resolve a different PATH from an agent command run through the user's login shell,
so shutil.which("lake") is not an adequate gate. Host authoring must configure its Lake cache first,
then prove through the login shell that the agent's Lake shim resolves a real executable with that final
environment. Review does not compile and must do neither. `tauceti doctor` must identify this explicitly.

Exit 0 = every case agrees; 1 = a mismatch."""

import contextlib
import io
import os
import sys
import tempfile
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


def opts(only):
    return tc.RoundOpts(only=only, agent="claude", work_model="claude", sandbox_host=True, dry_run=False)


_MISSING = object()


def replace(module, name, value, saved):
    saved.append((module, name, getattr(module, name, _MISSING)))
    setattr(module, name, value)


def restore(saved):
    for module, name, value in reversed(saved):
        if value is _MISSING:
            delattr(module, name)
        else:
            setattr(module, name, value)


# Pin the low-level probe independently from preflight: it must execute the login shell selected by
# _host_shell(), not inspect the worker process with shutil.which().
probe_helper = getattr(tc.agents, "host_login_shell_which", None)
check("host_login_shell_which helper exists", callable(probe_helper))
if callable(probe_helper):
    helper_saved = []
    helper_calls = []
    helper_rc = {"value": 0}
    helper_env = {"PATH": "/agent/path", "ELAN_HOME": "/shared/elan"}

    def fake_shell_run(argv, **kwargs):
        helper_calls.append((argv, kwargs))
        stdout = "/shared/elan/bin/lake\n" if helper_rc["value"] == 0 else ""
        return SimpleNamespace(returncode=helper_rc["value"], stdout=stdout, stderr="")

    try:
        replace(tc.agents, "_host_shell", lambda: "/the/login-shell", helper_saved)
        replace(tc.agents.subprocess, "run", fake_shell_run, helper_saved)
        resolved = probe_helper("lake", env=helper_env)
        check("Lake probe returns the login shell's resolved path", resolved == "/shared/elan/bin/lake")
        check(
            "Lake probe invokes the selected shell with -lc",
            bool(helper_calls)
            and helper_calls[-1][0][:2] == ["/the/login-shell", "-lc"]
            and "command -v lake" in helper_calls[-1][0][2]
            and "TAUCETI_LAKE_RESOLVE_ONLY=1" in helper_calls[-1][0][2]
            and str(REPO / "scripts" / "lake") in helper_calls[-1][0][2],
        )
        check("Lake probe receives the supplied agent environment", helper_calls[-1][1].get("env") == helper_env)

        helper_rc["value"] = 127
        check("failed login-shell lookup returns no path", probe_helper("lake", env=helper_env) is None)
    finally:
        restore(helper_saved)


# Exercise the real shim/helper/preflight handoff. A stale inherited TAUCETI_REAL_LAKE must be ignored,
# the executable discovered from the post-login PATH must replace it, and that exact value must reach
# host_agent_argv rather than failing only after model launch.
with tempfile.TemporaryDirectory() as real_td:
    real_root = Path(real_td)
    fake_bin = real_root / "bin"
    fake_bin.mkdir()
    fake_lake = fake_bin / "lake"
    fake_lake.write_text("#!/bin/sh\nexit 0\n")
    fake_lake.chmod(0o755)
    fake_shell = real_root / "login-shell"
    fake_shell.write_text('#!/bin/sh\n[ "$1" = "-lc" ] || exit 90\nexec /bin/bash -c "$2"\n')
    fake_shell.chmod(0o755)
    real_cfg = SimpleNamespace(
        wid="real-lake-preflight",
        home=real_root / "home",
        state=real_root / "state",
        checkout=real_root / "checkout",
        logdir=real_root / "logs",
    )
    real_cfg.home.mkdir()
    real_cfg.checkout.mkdir()
    real_keys = (
        "PATH",
        "TAUCETI_REAL_LAKE",
        "LAKE_CONFIG",
        "LAKE_CACHE_DIR",
        "LAKE_ARTIFACT_CACHE",
        "LAKE_RESTORE_ARTIFACTS",
    )
    real_env = {key: os.environ.get(key, _MISSING) for key in real_keys}
    real_saved = []
    try:
        os.environ["PATH"] = f"{fake_bin}:/usr/bin:/bin"
        os.environ["TAUCETI_REAL_LAKE"] = "/definitely/missing/stale-lake"
        replace(tc.agents, "_host_shell", lambda: str(fake_shell), real_saved)
        replace(tc.cli, "_have", lambda _tool: True, real_saved)
        tc.cli.preflight(real_cfg, opts(["fix"]))
        check("real shim ignores a stale inherited Lake path", os.environ["TAUCETI_REAL_LAKE"] == str(fake_lake))
        _, handed_env = tc.agents.host_agent_argv("", "claude")
        check(
            "successful real preflight hands discovered Lake to the agent",
            handed_env.get("TAUCETI_REAL_LAKE") == str(fake_lake),
        )
    finally:
        restore(real_saved)
        for key, value in real_env.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    CFG = SimpleNamespace(
        wid="lake-preflight",
        home=root / "home",
        state=root / "state",
        checkout=root / "checkout",
        logdir=root / "logs",
    )
    CFG.home.mkdir()
    CFG.state.mkdir()
    CFG.checkout.mkdir()

    cache_env = {
        "LAKE_CONFIG": str(root / "lake-config.toml"),
        "LAKE_CACHE_DIR": str(root / "cache"),
        "LAKE_ARTIFACT_CACHE": "1",
        "LAKE_RESTORE_ARTIFACTS": "1",
    }
    base_agent_env = {
        "HOME": str(root / "agent-home"),
        "ELAN_HOME": str(root / "shared-elan"),
        "PATH": "/operator/bin:/usr/bin",
    }
    base_marker = "inherited-by-agent"
    tested_env_keys = (*base_agent_env, *cache_env, "TAUCETI_TEST_AGENT_ENV", "TAUCETI_REAL_LAKE")
    saved_agent_env = {key: os.environ.get(key, _MISSING) for key in tested_env_keys}
    os.environ.update(base_agent_env)
    os.environ["TAUCETI_TEST_AGENT_ENV"] = base_marker

    saved = []
    calls = []
    login_result = {"path": None}
    parent_lake = {"present": True}

    def fake_have(tool):
        calls.append(("which", tool))
        return parent_lake["present"] if tool == "lake" else True

    def fake_configure(cfg):
        calls.append(("configure", cfg))
        # Match the real helper's contract: materialize/update the process environment and return the
        # four-variable Lake overlay that host_agent_argv will inherit.
        os.environ.update(cache_env)
        return dict(cache_env)

    def fake_lake_env(cfg):
        calls.append(("lake-env", cfg))
        return dict(cache_env)

    def fake_login_which(tool, env=None):
        calls.append(("login", tool, env))
        return login_result["path"]

    try:
        # Patch both defining and consuming modules, making the test robust whether cli.py imports the
        # helpers directly or reaches them through agents.
        replace(tc.cli, "_have", fake_have, saved)
        for module in (tc.agents, tc.cli):
            replace(module, "configure_host_lake_cache", fake_configure, saved)
            replace(module, "host_lake_env", fake_lake_env, saved)
            replace(module, "host_login_shell_which", fake_login_which, saved)

        # 1) Parent which() says Lake exists, but the agent's login shell cannot resolve it: fail closed.
        calls.clear()
        parent_lake["present"] = True
        login_result["path"] = None
        raised, message = False, ""
        try:
            tc.cli.preflight(CFG, opts(["fix"]))
        except tc.Die as exc:
            raised, message = True, str(exc)
        check("host authoring trusts failed agent-shell probe over parent which()", raised)
        check(
            "host authoring failure names Lake and the agent login shell",
            "lake" in message.lower() and "shell" in message.lower(),
        )
        event_names = [call[0] for call in calls]
        check(
            "host cache is configured before the login-shell probe",
            "configure" in event_names
            and "login" in event_names
            and event_names.index("configure") < event_names.index("login"),
        )
        login_calls = [call for call in calls if call[0] == "login"]
        received = (login_calls[-1][2] if login_calls else None) or {}
        check("preflight probes exactly `lake`", bool(login_calls) and login_calls[-1][1] == "lake")
        check(
            "probe receives all required host Lake variables",
            all(received.get(key) == value for key, value in cache_env.items()),
        )
        check(
            "probe receives the inherited base agent environment too",
            received.get("TAUCETI_TEST_AGENT_ENV") == base_marker,
        )
        _, launched_env = tc.agents.host_agent_argv("", "claude")
        exact_agent_keys = ("PATH", "HOME", "ELAN_HOME", "TAUCETI_LAKE", "TAUCETI_TRUSTED_RUN", *cache_env)
        check(
            "preflight probes the exact PATH/HOME/Elan/Lake environment passed to the host agent",
            all(key in received and received[key] == launched_env.get(key) for key in exact_agent_keys),
        )

        # 2) The inverse disagreement: parent which() says Lake is absent, but the login shell resolves it.
        # The parent lookup must not veto the environment the model will actually use.
        calls.clear()
        parent_lake["present"] = False
        login_result["path"] = "/shared/elan/bin/lake"
        raised = False
        try:
            tc.cli.preflight(CFG, opts(["fix"]))
        except tc.Die:
            raised = True
        check("agent-shell Lake succeeds even when parent which() would fail", not raised)
        check("preflight never asks parent shutil.which() about Lake", ("which", "lake") not in calls)
        check(
            "successful mocked preflight exports the discovered real Lake",
            os.environ.get("TAUCETI_REAL_LAKE") == "/shared/elan/bin/lake",
        )
        _, successful_agent_env = tc.agents.host_agent_argv("", "claude")
        check(
            "mocked successful Lake path reaches host agent argv",
            successful_agent_env.get("TAUCETI_REAL_LAKE") == "/shared/elan/bin/lake",
        )

        # 3) A host review runs the fetched review engine and never compiles: no cache setup and no Lake
        # probe, even when both would fail.
        calls.clear()
        parent_lake["present"] = False
        login_result["path"] = None
        raised = False
        try:
            tc.cli.preflight(CFG, opts(["review"]))
        except tc.Die:
            raised = True
        check("host review without Lake is not blocked", not raised)
        check("host review does not configure a Lake cache", not any(call[0] == "configure" for call in calls))
        check("host review does not probe the login shell for Lake", not any(call[0] == "login" for call in calls))

        # 4) Doctor must make the distinction visible. Parent PATH says yes, agent shell says no; its Lake
        # row must still be MISSING and explicitly name the agent shell. Doctor only describes the cache
        # env and must not materialize it.
        calls.clear()
        parent_lake["present"] = True
        login_result["path"] = None
        replace(tc.cli.Config, "resolve", staticmethod(lambda *a, **k: CFG), saved)
        replace(
            tc.cli.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
            saved,
        )
        replace(tc.cli, "_claude_keychain_creds", lambda: None, saved)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            tc.cli.cmd_doctor(SimpleNamespace())
        doctor_lines = output.getvalue().splitlines()
        lake_lines = [line for line in doctor_lines if "lake (agent shell)" in line.lower()]
        check("doctor explicitly labels Lake as an agent-shell check", bool(lake_lines))
        check("doctor reports failed agent-shell Lake as MISSING", bool(lake_lines) and "MISSING" in lake_lines[0])
        check("doctor does not materialize host cache configuration", not any(call[0] == "configure" for call in calls))
    finally:
        restore(saved)
        for key, value in saved_agent_env.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

sys.exit(1 if fails else 0)
