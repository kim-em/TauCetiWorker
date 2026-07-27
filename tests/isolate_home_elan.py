#!/usr/bin/env python3
"""Per-worker HOME isolation must isolate credentials, not the host's Elan installation.

Elan derives its default installation root from $HOME.  Repointing HOME without pinning ELAN_HOME
therefore makes every worker look under its private home for toolchains, even though the operator
already has a shared installation.  isolate_home() must preserve an explicit ELAN_HOME; when none was
exported, it must resolve the default from the real, pre-isolation HOME and pass that value to the host
agent unchanged.

This harness uses temporary real/isolated homes and Linux behavior, so it neither reads real credentials
nor touches the macOS Keychain.  Exit 0 = every assertion holds; 1 = a mismatch.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0


def check(name, condition):
    global fails
    fails += not condition
    print(f"[{'OK ' if condition else 'BAD'}] {name}")


_MISSING = object()
_ENV_KEYS = (
    "HOME",
    "ELAN_HOME",
    "CLAUDE_CONFIG_DIR",
    "GH_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def exercise_isolation(root: Path, *, explicit_elan: Path | None) -> tuple[Path, Path, dict[str, str]]:
    """Run one isolated-home case and return (real home, isolated home, host-agent env)."""
    real = root / "real-home"
    isolated = root / "worker-home"
    real.mkdir(parents=True)
    # A marker makes the intended shared installation concrete; isolate_home must leave it in place and
    # must not manufacture the corresponding private tree.
    shared_toolchain = real / ".elan" / "toolchains" / "shared"
    shared_toolchain.mkdir(parents=True)
    (shared_toolchain / "marker").write_text("shared")

    saved_env = {key: os.environ.get(key, _MISSING) for key in _ENV_KEYS}
    saved_platform = tc.agents.sys.platform
    saved_worker_home = tc.agents._worker_iso_home
    saved_host_home = tc.agents._host_home
    try:
        os.environ["HOME"] = str(real)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("GH_CONFIG_DIR", None)
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        if explicit_elan is None:
            os.environ.pop("ELAN_HOME", None)
        else:
            os.environ["ELAN_HOME"] = str(explicit_elan)

        # Linux avoids macOS's Keychain/Colima setup; the injected path keeps all writes in the temp tree.
        tc.agents.sys.platform = "linux"
        tc.agents._worker_iso_home = lambda wid: isolated
        got = tc.agents.isolate_home("elan-test")
        _, agent_env = tc.agents.host_agent_argv("", "codex")

        check("isolate_home returns the injected worker home", got == isolated)
        check("HOME moves to the per-worker credential home", os.environ.get("HOME") == str(isolated))
        check("shared toolchain marker remains intact", (shared_toolchain / "marker").read_text() == "shared")
        check("isolation creates no private .elan tree", not (isolated / ".elan").exists())
        if explicit_elan is None:
            # Simulate a child inherited from an older already-isolated launcher that omitted
            # ELAN_HOME. The idempotent early-return path must still recover the real login home.
            os.environ.pop("ELAN_HOME", None)
            tc.agents._host_home = lambda: real
            repeated = tc.agents.isolate_home("elan-test")
            _, agent_env = tc.agents.host_agent_argv("", "codex")
            check("already-isolated HOME remains idempotent", repeated == isolated)
            check("already-isolated child recovers shared ELAN_HOME", agent_env.get("ELAN_HOME") == str(real / ".elan"))
            check("already-isolated child still creates no private .elan", not (isolated / ".elan").exists())
        return real, isolated, agent_env
    finally:
        tc.agents.sys.platform = saved_platform
        tc.agents._worker_iso_home = saved_worker_home
        tc.agents._host_home = saved_host_home
        for key, value in saved_env.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


with tempfile.TemporaryDirectory() as td:
    real, _isolated, agent_env = exercise_isolation(Path(td) / "default", explicit_elan=None)
    expected = str(real / ".elan")
    check("unset ELAN_HOME resolves from the real pre-isolation HOME", agent_env.get("ELAN_HOME") == expected)

    explicit = Path(td) / "operator-selected-elan"
    _real, _isolated, agent_env = exercise_isolation(Path(td) / "explicit", explicit_elan=explicit)
    check("an explicit ELAN_HOME is preserved exactly", agent_env.get("ELAN_HOME") == str(explicit))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)
