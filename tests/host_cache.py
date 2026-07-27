#!/usr/bin/env python3
"""Host authoring warms both Lean caches under trusted main before launching an agent.

This pins the host-only contract: the generated public Lake configuration and cache directory are
outside isolated HOME, all Lake restore variables reach the agent, Mathlib download failure is fatal
after one retry, a TauCeti cache miss is advisory, and no eager full build is introduced.  It also
guards the dispatch ordering that keeps machine-wide setup failures out of fix-CI attempt counters.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0
LAKE_KEYS = (
    "LAKE_CONFIG",
    "LAKE_CACHE_DIR",
    "LAKE_ARTIFACT_CACHE",
    "LAKE_RESTORE_ARTIFACTS",
)


def check(name, ok):
    global fails
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}")


def restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def temp_cfg(root: Path):
    return SimpleNamespace(
        state=root / "state",
        checkout=root / "checkout",
        home=root / "home",
        logdir=root / "logs",
        wid="host-cache-test",
    )


# The host owns the service configuration.  It must not fall back to the isolated HOME's ~/.lake,
# and later shells spawned by the model must see the exact same settings used by the trusted fetch.
saved_lake_env = {key: os.environ.get(key) for key in LAKE_KEYS}
try:
    for key in LAKE_KEYS:
        os.environ[key] = f"/stale/operator/value/{key}"
    with tempfile.TemporaryDirectory() as td:
        cfg = temp_cfg(Path(td))
        env = tc.configure_host_lake_cache(cfg)
        config_path = Path(env["LAKE_CONFIG"])
        cache_path = Path(env["LAKE_CACHE_DIR"])
        config = config_path.read_text()

        check("Lake config path is absolute", config_path.is_absolute())
        check(
            "Lake config lives under per-worker state",
            config_path == cfg.state or cfg.state in config_path.parents,
        )
        check("Lake cache path is absolute", cache_path.is_absolute())
        check("Lake cache is checkout-local", cache_path == cfg.checkout / ".lake" / "cache")
        check("Lake artifact cache is enabled", env["LAKE_ARTIFACT_CACHE"] == "true")
        check("Lake restores artifacts during later builds", env["LAKE_RESTORE_ARTIFACTS"] == "true")
        check("public config selects TauCeti service", 'cache.defaultService = "tauceti-public"' in config)
        check("public config has one service block", config.count("[[cache.service]]") == 1)
        check("public config names TauCeti service", 'name = "tauceti-public"' in config)
        check("public config uses the S3 service kind", 'kind = "s3"' in config)
        check(
            "public config has the canonical artifact endpoint",
            f'artifactEndpoint = "{tc.TAUCETI_CACHE_ARTIFACT_URL}"' in config,
        )
        check(
            "public config has the canonical revision endpoint",
            f'revisionEndpoint = "{tc.TAUCETI_CACHE_REVISION_URL}"' in config,
        )
        check("public config contains only the two canonical endpoints", config.count(tc.TAUCETI_CACHE_DOMAIN) == 2)
        check("configure updates the process environment", all(os.environ.get(key) == env[key] for key in LAKE_KEYS))

        _, agent_env = tc.host_agent_argv("PROMPT", "codex")
        check("host agent inherits every Lake cache variable", all(agent_env.get(key) == env[key] for key in LAKE_KEYS))
        check("host agent inherits an absolute Lake launcher", Path(agent_env.get("TAUCETI_LAKE", "")).is_absolute())
finally:
    restore_env(saved_lake_env)


def exercise_prepare(mathlib_rcs, tauceti_rc):
    """Run prepare_host_authoring with checkout/network effects replaced by a command recorder."""
    root = Path(tempfile.mkdtemp())
    cfg = temp_cfg(root)
    order = []
    calls = []
    mathlib_rcs = iter(mathlib_rcs)
    saved_prepare_checkout = tc.agents.prepare_checkout
    saved_run = tc.agents.subprocess.run
    saved_env = {key: os.environ.get(key) for key in LAKE_KEYS}

    def fake_prepare_checkout(got):
        check("prepare receives the requested worker config", got is cfg)
        order.append("prepare-main")
        return True

    def fake_run(argv, **kwargs):
        rendered = " ".join(str(arg) for arg in argv)
        if "lake exe cache get" in rendered:
            kind, rc = "mathlib", next(mathlib_rcs)
        elif "lake cache get" in rendered:
            kind, rc = "tauceti", tauceti_rc
        else:
            kind, rc = "other", 0
        order.append(kind)
        calls.append((kind, list(argv), rendered, kwargs))
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    tc.agents.prepare_checkout = fake_prepare_checkout
    tc.agents.subprocess.run = fake_run
    try:
        result = tc.prepare_host_authoring(cfg)
        error = None
    except Exception as exc:
        result, error = None, exc
    finally:
        tc.agents.prepare_checkout = saved_prepare_checkout
        tc.agents.subprocess.run = saved_run
        restore_env(saved_env)
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return result, error, order, calls


# Successful trusted setup: current main first, then Mathlib, then TauCeti.  A public cache miss only
# means more compilation later, so it must not prevent the semantic repair agent from launching.
_, error, order, calls = exercise_prepare([0], 1)
check("TauCeti cache miss is nonfatal", error is None)
check("trusted main is prepared before either cache fetch", order[:3] == ["prepare-main", "mathlib", "tauceti"])
check("host setup runs no unexpected subprocess", "other" not in order)
check(
    "host setup never runs a pre-agent lake build",
    all("lake build" not in command for _, _, command, _ in calls),
)
check(
    "TauCeti fetch uses the named public service and canonical repository",
    any(
        kind == "tauceti"
        and "cache get" in command
        and f"--service {tc.TAUCETI_CACHE_SERVICE}" in command
        and f"--repo {tc.TAUCETI}" in command
        for kind, _, command, _ in calls
    ),
)
check(
    "cache commands run in the host checkout",
    all(Path(str(kwargs.get("cwd"))).resolve() == Path(str(calls[0][3]["cwd"])).resolve() for _, _, _, kwargs in calls)
    and Path(str(calls[0][3]["cwd"])).resolve().name == "checkout",
)
check(
    "cache commands receive all Lake variables",
    all(all((kwargs.get("env") or {}).get(key) for key in LAKE_KEYS) for _, _, _, kwargs in calls),
)
check(
    "cache commands run through a login shell",
    all(len(argv) >= 3 and argv[-2] == "-lc" for _, argv, _, _ in calls),
)

# A transient Mathlib outage gets one retry.  Once the retry succeeds, the TauCeti fetch still follows.
_, error, order, calls = exercise_prepare([1, 0], 0)
check("Mathlib cache gets one retry", error is None and order == ["prepare-main", "mathlib", "mathlib", "tauceti"])
check(
    "retry path still has no full build",
    all("lake build" not in command for _, _, command, _ in calls),
)

# Two Mathlib failures are a machine/setup failure: fail before touching TauCeti or launching a model.
_, error, order, calls = exercise_prepare([1, 1], 0)
check("repeated Mathlib failure raises Die", isinstance(error, tc.Die))
check("fatal Mathlib path stops before TauCeti", order == ["prepare-main", "mathlib", "mathlib"])
check(
    "fatal Mathlib path still has no full build",
    all("lake build" not in command for _, _, command, _ in calls),
)


# dispatch() is the counter boundary.  Host preparation must happen before do_fix_ci, whose real
# implementation charges both semantic counters and eventually launches the model.
wu = tc.work_units
saved_dispatch_bits = {
    name: getattr(wu, name)
    for name in (
        "prepare_host_authoring",
        "_host_agent_binary",
        "do_fix_ci",
        "_progress_snapshot",
        "_progressed",
    )
}
events = []


class RecordingCounters:
    def __init__(self):
        self.keys = []

    def incr(self, key):
        self.keys.append(key)


counters = RecordingCounters()
w = SimpleNamespace(cfg=SimpleNamespace(), counters=counters)
c = tc.Candidate(123, "deadbeef", "red CI")
opts = tc.RoundOpts(
    only=["fix-ci"],
    agent="codex",
    work_model="codex",
    sandbox_host=True,
    dry_run=False,
)


def fail_machine_setup(_cfg):
    events.append("prepare")
    raise tc.Die("host cache unavailable")


def fake_fix_ci(worker, _sv, candidate, _opts, _bubble):
    events.append("model")
    worker.counters.incr(f"ci-{candidate.pr}-{candidate.head[:12]}")
    worker.counters.incr(f"ci-pr-{candidate.pr}")
    return 0


try:
    wu.prepare_host_authoring = fail_machine_setup
    wu._host_agent_binary = lambda _stage, _model: None
    wu.do_fix_ci = fake_fix_ci
    wu._progress_snapshot = lambda *_args: None
    wu._progressed = lambda *_args: True
    try:
        wu.dispatch("fix-ci", w, SimpleNamespace(), c, opts)
        dispatch_error = None
    except Exception as exc:
        dispatch_error = exc
    check("dispatch surfaces host setup failure", isinstance(dispatch_error, tc.Die))
    check("dispatch attempts host setup exactly once", events == ["prepare"])
    check("host setup failure launches no model", "model" not in events)
    check("host setup failure charges no fix-CI counter", counters.keys == [])
finally:
    for name, value in saved_dispatch_bits.items():
        setattr(wu, name, value)

# A claim race returns None so run_round can try another candidate in the same stage. The immutable
# current-main checkout and caches are already warm; dispatch must reuse them rather than fetching
# both caches again before the second claim.
reuse_saved = {
    name: getattr(wu, name)
    for name in ("prepare_host_authoring", "_host_agent_binary", "do_fix_ci", "_progress_snapshot")
}
reuse_events = []
reuse_opts = tc.RoundOpts(
    only=["fix-ci"],
    agent="codex",
    work_model="codex",
    sandbox_host=True,
    dry_run=False,
)


def record_prepare(_cfg):
    reuse_events.append("prepare")


def claimed_then_run(*_args):
    reuse_events.append("stage")
    return None if reuse_events.count("stage") == 1 else 1


try:
    wu.prepare_host_authoring = record_prepare
    wu._host_agent_binary = lambda _stage, _model: None
    wu.do_fix_ci = claimed_then_run
    wu._progress_snapshot = lambda *_args: None
    first = wu.dispatch("fix-ci", SimpleNamespace(cfg=SimpleNamespace()), SimpleNamespace(), c, reuse_opts)
    second = wu.dispatch("fix-ci", SimpleNamespace(cfg=SimpleNamespace()), SimpleNamespace(), c, reuse_opts)
    check("claim-raced candidate asks the caller to continue", first is None)
    check("second candidate completes without another host warmup", second == 1)
    check("claim-raced candidates reuse one host cache preparation", reuse_events == ["prepare", "stage", "stage"])
finally:
    for name, value in reuse_saved.items():
        setattr(wu, name, value)


print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
