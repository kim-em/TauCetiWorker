#!/usr/bin/env python3
"""Bubble work rounds warm Mathlib's cache and TauCeti's Lake cache before launching the agent.

The review/probe path supplies its own command and must remain untouched: it does not compile and should
not gain project-cache egress. Exit 0 = the bootstrap/config/argv contract agrees; 1 = a mismatch.
"""

import contextlib
import io
import os
import shutil
import subprocess
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


inner = "env OPENAI_API_KEY= agent --do-work"
bootstrap = tc.bubble_work_cmd(inner)
mathlib_i = bootstrap.index("lake exe cache get")
tauceti_i = bootstrap.index("lake cache get")
build_i = bootstrap.index("lake build")
agent_i = bootstrap.index("exec " + inner)
check("cache/build/agent order", mathlib_i < tauceti_i < build_i < agent_i)
check("TauCeti cache uses the canonical repository", f"--repo {tc.TAUCETI}" in bootstrap)
check("agent inherits Lake restore settings", "LAKE_RESTORE_ARTIFACTS=true" in bootstrap)
check("public config selects the named service", 'cache.defaultService = "tauceti-public"' in tc.TAUCETI_CACHE_CONFIG)
check("public config uses only the allowed host", tc.TAUCETI_CACHE_CONFIG.count(tc.TAUCETI_CACHE_DOMAIN) == 2)


# Execute the bootstrap against a fake `lake`, so the tests pin the failure semantics rather than just
# matching shell text. A red preliminary build must still launch the repair agent; a Mathlib-cache
# outage must not. The fake records each invocation so the one retry is also observable.
shimdir = Path(tempfile.mkdtemp())
marker = shimdir / "agent-ran"
calls = shimdir / "lake-calls"
lake = shimdir / "lake"
lake.write_text(
    "#!/bin/sh\n"
    'printf "%s\\n" "$*" >> "$LAKE_CALLS"\n'
    'case "$*" in\n'
    '  "exe cache get") exit "${MATHLIB_RC:-0}" ;;\n'
    '  cache\\ get*) exit "${TAUCETI_RC:-0}" ;;\n'
    '  build) exit "${BUILD_RC:-0}" ;;\n'
    "esac\n"
    "exit 99\n"
)
lake.chmod(0o755)


def run_bootstrap(*, mathlib=0, tauceti=0, build=0):
    marker.unlink(missing_ok=True)
    calls.unlink(missing_ok=True)
    env = {
        **os.environ,
        "PATH": f"{shimdir}:{os.environ.get('PATH', '')}",
        "LAKE_CALLS": str(calls),
        "MATHLIB_RC": str(mathlib),
        "TAUCETI_RC": str(tauceti),
        "BUILD_RC": str(build),
        "MARKER": str(marker),
    }
    cmd = tc.bubble_work_cmd("sh -c 'touch \"$MARKER\"'")
    result = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True)
    return result, calls.read_text().splitlines()


result, lake_calls = run_bootstrap(mathlib=1)
check("Mathlib cache failure blocks the agent", result.returncode != 0 and not marker.exists())
check("Mathlib cache gets one retry", lake_calls == ["exe cache get", "exe cache get"])

result, _ = run_bootstrap(tauceti=1)
check("TauCeti cache miss still launches the agent", result.returncode == 0 and marker.exists())

result, _ = run_bootstrap(build=1)
check("red preliminary build still launches the repair agent", result.returncode == 0 and marker.exists())
check("red preliminary build emits a warning", "agent starts from a red tree" in result.stderr)

shutil.rmtree(shimdir, ignore_errors=True)


# Cache isolation is load-bearing: a failed Bubble setting command must not leave a sentinel that makes
# later rounds assume overlay mode. Conversely, an already-verified config needs no subprocess.
cache_home = Path(tempfile.mkdtemp())
saved_bubble_cmd = tc.agents.bubble_cmd
old_bubble_home = os.environ.get("TAUCETI_BUBBLE_HOME")
os.environ["TAUCETI_BUBBLE_HOME"] = str(cache_home)
cache_cfg = SimpleNamespace(home=cache_home, wid="cache-test")
try:
    tc.agents.bubble_cmd = lambda: ["false"]
    try:
        tc.ensure_bubble_home(cache_cfg)
        blocked = False
    except tc.Die:
        blocked = True
    check("failed overlay configuration blocks the round", blocked)
    check("failed overlay configuration writes no trust sentinel", not (cache_home / ".worker-init").exists())

    (cache_home / "config.toml").write_text('[security]\nshared_cache = "overlay"\n')
    tc.agents.bubble_cmd = lambda: (_ for _ in ()).throw(AssertionError("unexpected Bubble subprocess"))
    env = tc.ensure_bubble_home(cache_cfg)
    check("verified overlay configuration is accepted directly", env["BUBBLE_HOME"] == str(cache_home))
finally:
    tc.agents.bubble_cmd = saved_bubble_cmd
    if old_bubble_home is None:
        os.environ.pop("TAUCETI_BUBBLE_HOME", None)
    else:
        os.environ["TAUCETI_BUBBLE_HOME"] = old_bubble_home
    shutil.rmtree(cache_home, ignore_errors=True)


# Exercise run_in_bubble through its no-side-effect echo path to pin which rounds get the egress flag
# and bootstrap. The normal credential mirror/model launch happens after this early return.
tmp = Path(tempfile.mkdtemp())
cfg = SimpleNamespace(
    state=tmp / "state",
    home=tmp / "home",
    wid="cache-test",
    logdir=tmp / "logs",
)
w = SimpleNamespace(cfg=cfg)
opts = SimpleNamespace(work_model="claude")
saved_home = tc.agents.ensure_bubble_home
saved_pop = tc.agents._bubble_pop
tc.agents.ensure_bubble_home = lambda _cfg: dict(os.environ)
tc.agents._bubble_pop = lambda _cfg, _env: None
os.environ["TAUCETI_AGENT_ECHO"] = "1"
try:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "PROMPT", opts)
    work_argv = out.getvalue()
    check("work echo succeeds", rc == 0)
    check("work round allows only the TauCeti cache host", f"--allow-domain {tc.TAUCETI_CACHE_DOMAIN}" in work_argv)
    check("work round runs both cache commands", "lake exe cache get" in work_argv and "lake cache get" in work_argv)
    check(
        "staged Lake config matches the public config",
        (cfg.state / "bubble-round" / "lake-cache.toml").read_text() == tc.TAUCETI_CACHE_CONFIG,
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = tc.run_in_bubble(w, tc.TAUCETI, "", opts, inner_cmd="true", cred_model="claude")
    review_argv = out.getvalue()
    check("review/probe echo succeeds", rc == 0)
    check("review/probe does not add cache egress", "--allow-domain" not in review_argv)
    check("review/probe command is not wrapped in a build", "lake exe cache get" not in review_argv)
finally:
    os.environ.pop("TAUCETI_AGENT_ECHO", None)
    tc.agents.ensure_bubble_home = saved_home
    tc.agents._bubble_pop = saved_pop
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
