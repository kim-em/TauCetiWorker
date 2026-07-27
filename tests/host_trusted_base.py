#!/usr/bin/env python3
"""Host PR authoring builds a live PR source overlay in a detached trusted-main worktree.

The writable PR checkout must keep ordinary Git semantics: its stale configuration stays in its
branch, status/index remain clean, merge and rebase work, branch claims reach the agent, and
git-safe-push still leases against the checked-out PR OID. Lake instead runs from a detached
origin/main worktree whose real TauCeti directory is refreshed from the live PR checkout before each
Lake command and whose .lake points at the warm checkout cache. A validated bump copies only its PR
pins into that trusted view. No network or Lean installation is required.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

fails = 0
_MISSING = object()
_GIT_ENV_KEYS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def run(*argv: str | Path, cwd: Path, check_rc: bool = True, env: dict[str, str] | None = None):
    p = subprocess.run(
        [str(a) for a in argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if check_rc and p.returncode:
        raise RuntimeError(f"{' '.join(map(str, argv))} failed ({p.returncode}): {p.stderr or p.stdout}")
    return p


def git(co: Path, *args: str, check_rc: bool = True):
    return run("git", *args, cwd=co, check_rc=check_rc)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(co: Path, message: str) -> str:
    git(co, "add", "-A")
    git(co, "commit", "-q", "-m", message)
    return git(co, "rev-parse", "HEAD").stdout.strip()


def make_fixture(tag: str) -> tuple[SimpleNamespace, Path, str]:
    """Return ``(cfg, bare_remote, stale_pr_oid)`` with main ahead of the PR."""
    root = Path(tempfile.mkdtemp(prefix=f"host-trusted-{tag}-"))
    remote = root / "remote.git"
    co = root / "checkout"
    run("git", "init", "-q", "--bare", remote, cwd=root)
    run("git", "init", "-q", "-b", "main", co, cwd=root)
    git(co, "config", "user.name", "TauCeti test")
    git(co, "config", "user.email", "test@example.invalid")
    git(co, "remote", "add", "origin", str(remote))

    write(co / ".gitignore", ".lake/\n")
    write(co / "lakefile.toml", "lakefile = 'old-pr-base'\n")
    write(co / "lake-manifest.json", '{"revision":"old-pr-base"}\n')
    write(co / "lean-toolchain", "lean-old-pr-base\n")
    write(co / "scripts/lint-env.sh", "echo old-pr-base-lint\n")
    write(co / "scripts/obsolete.sh", "echo still-present-on-pr\n")
    write(co / "TauCeti/Shared.lean", "module\n-- old common source\n")
    write(co / "TauCeti/DeletedByPr.lean", "module\n-- deleted on the PR\n")
    write(co / "TauCeti.lean", "-- old root\n")
    commit(co, "base")
    git(co, "push", "-q", "-u", "origin", "main")

    git(co, "switch", "-q", "-c", "stale-pr")
    write(co / "TauCeti/Shared.lean", "module\n-- exact PR source\n")
    write(co / "TauCeti/PrOnly.lean", "module\n-- PR-only source\n")
    (co / "TauCeti/DeletedByPr.lean").unlink()
    write(co / "TauCeti.lean", "-- exact PR root\n")
    pr_oid = commit(co, "PR source")
    git(co, "push", "-q", "-u", "origin", "stale-pr")

    # Current main changes every trusted build input and adds source that CI's wholesale PR overlay
    # must initially exclude. It deliberately does not touch Shared.lean, so a later merge/rebase
    # regression can prove integration succeeds rather than merely avoiding a dirty-config error.
    git(co, "switch", "-q", "main")
    write(co / "lakefile.toml", "lakefile = 'trusted-main'\n")
    write(co / "lake-manifest.json", '{"revision":"trusted-main"}\n')
    write(co / "lean-toolchain", "lean-trusted-main\n")
    write(co / "scripts/lint-env.sh", "echo trusted-main-lint\n")
    (co / "scripts/obsolete.sh").unlink()
    write(co / "scripts/main-only.sh", "echo trusted-main-only\n")
    write(co / "TauCeti/MainOnly.lean", "module\n-- current-main-only source\n")
    commit(co, "advance main config and source")
    git(co, "push", "-q", "origin", "main")

    write(co / ".lake/warm-current-main", "warm artifact\n")
    git(co, "switch", "-q", "stale-pr")
    cfg = SimpleNamespace(checkout=co, state=root / "state", logdir=root / "logs")
    return cfg, remote, pr_oid


def contents_at(co: Path, rev: str, path: str) -> str:
    return git(co, "show", f"{rev}:{path}").stdout


def assert_pr_worktree_untouched(cfg: SimpleNamespace, pr_oid: str) -> None:
    co = cfg.checkout
    check("PR branch remains checked out", git(co, "branch", "--show-current").stdout.strip() == "stale-pr")
    check("trusted staging does not move PR HEAD", git(co, "rev-parse", "HEAD").stdout.strip() == pr_oid)
    check(
        "PR lakefile remains stale in its writable worktree",
        (co / "lakefile.toml").read_text() == "lakefile = 'old-pr-base'\n",
    )
    check(
        "PR manifest remains in its writable worktree",
        (co / "lake-manifest.json").read_text() == '{"revision":"old-pr-base"}\n',
    )
    check(
        "PR toolchain remains in its writable worktree",
        (co / "lean-toolchain").read_text() == "lean-old-pr-base\n",
    )
    check(
        "PR Shared source remains active",
        (co / "TauCeti/Shared.lean").read_text() == "module\n-- exact PR source\n",
    )
    check("PR-only source remains active", (co / "TauCeti/PrOnly.lean").is_file())
    check("PR deletion remains active", not (co / "TauCeti/DeletedByPr.lean").exists())
    check("main-only source is absent from the PR overlay", not (co / "TauCeti/MainOnly.lean").exists())
    check("PR root TauCeti.lean remains active", (co / "TauCeti.lean").read_text() == "-- exact PR root\n")
    check("trusted view leaves writable PR status clean", git(co, "status", "--porcelain").stdout == "")
    git(co, "add", "-A")
    check("git add -A cannot stage trusted-view files", git(co, "diff", "--cached", "--name-only").stdout == "")


def assert_trusted_view(cfg: SimpleNamespace, *, bump: bool) -> Path:
    co = cfg.checkout
    build = cfg.state / "trusted-build"
    check("trusted build environment names the detached view", os.environ.get("TAUCETI_TRUSTED_BUILD") == str(build))
    check(
        "trusted view is detached at current origin/main",
        git(build, "rev-parse", "HEAD").stdout.strip()
        == git(co, "rev-parse", "refs/remotes/origin/main").stdout.strip(),
    )
    check(
        "trusted lakefile comes from main",
        (build / "lakefile.toml").read_text() == "lakefile = 'trusted-main'\n",
    )
    check(
        "trusted lint script comes from main",
        (build / "scripts/lint-env.sh").read_text() == "echo trusted-main-lint\n",
    )
    check("trusted main-only script is available", (build / "scripts/main-only.sh").is_file())
    check("script removed on main stays absent", not (build / "scripts/obsolete.sh").exists())
    expected_rev = "old-pr-base" if bump else "trusted-main"
    expected_toolchain = "lean-old-pr-base" if bump else "lean-trusted-main"
    check(
        "trusted view selects the intended manifest",
        (build / "lake-manifest.json").read_text() == f'{{"revision":"{expected_rev}"}}\n',
    )
    check(
        "trusted view selects the intended toolchain",
        (build / "lean-toolchain").read_text() == f"{expected_toolchain}\n",
    )
    check("trusted TauCeti tree is a traversable real directory", (build / "TauCeti").is_dir())
    check("trusted TauCeti tree is not a directory symlink", not (build / "TauCeti").is_symlink())
    check(
        "trusted source snapshot has exact PR contents",
        (build / "TauCeti/Shared.lean").read_text() == (co / "TauCeti/Shared.lean").read_text(),
    )
    check("trusted root module is a regular snapshot", (build / "TauCeti.lean").is_file())
    check("trusted root module has exact PR contents", (build / "TauCeti.lean").read_text() == "-- exact PR root\n")
    check("trusted view sees PR-only source", (build / "TauCeti/PrOnly.lean").is_file())
    check("trusted view sees the PR deletion", not (build / "TauCeti/DeletedByPr.lean").exists())
    check("trusted view excludes main-only source before integration", not (build / "TauCeti/MainOnly.lean").exists())
    enumerated = run("find", "TauCeti", "-name", "*.lean", cwd=build).stdout.splitlines()
    check(
        "current-main lint-style find traverses every PR module",
        sorted(enumerated) == ["TauCeti/PrOnly.lean", "TauCeti/Shared.lean"],
    )
    check("trusted view shares the warmed .lake tree", (build / ".lake").resolve() == (co / ".lake").resolve())
    check(
        "warm current-main artifact survives",
        (build / ".lake/warm-current-main").read_text() == "warm artifact\n",
    )

    # Use host_agent_argv's actual environment handoff, then invoke the packaged command path with a
    # fake Lake. This pins trusted-view inheritance and exact argument forwarding without Lean.
    fake_lake = cfg.state / "fake-real-lake"
    write(fake_lake, '#!/bin/sh\nprintf "cwd=%s\\n" "$PWD"\nprintf "arg=%s\\n" "$@"\n')
    fake_lake.chmod(0o755)
    _, env = tc.agents.host_agent_argv("", "codex")
    env["TAUCETI_REAL_LAKE"] = str(fake_lake)
    routed = run(REPO / "scripts/lake", "build", "--iofail", cwd=co, env=env)
    lines = routed.stdout.splitlines()
    check("host agent inherits trusted-build routing", env.get("TAUCETI_TRUSTED_BUILD") == str(build))
    check("agent Lake shim runs the real command from the trusted view", lines[:1] == [f"cwd={build}"])
    check("agent Lake shim forwards every argument", lines[1:] == ["arg=build", "arg=--iofail"])
    trusted_script = run(env["TAUCETI_TRUSTED_RUN"], "bash", "scripts/lint-env.sh", cwd=co, env=env)
    check("host agent inherits the trusted-script launcher", env["TAUCETI_TRUSTED_RUN"].endswith("/trusted-run"))
    check("trusted-script launcher executes current-main tooling", trusted_script.stdout.strip() == "trusted-main-lint")
    return build


def test_normal_view_and_safe_push() -> None:
    cfg, remote, pr_oid = make_fixture("normal")
    co = cfg.checkout
    old_expect = os.environ.get("TAUCETI_PUSH_EXPECT")
    os.environ["TAUCETI_PUSH_EXPECT"] = pr_oid
    try:
        check("normal trusted-base staging succeeds", tc.agents.stage_trusted_base_config(cfg))
        check("staging preserves safe-push expected OID", os.environ.get("TAUCETI_PUSH_EXPECT") == pr_oid)
        assert_pr_worktree_untouched(cfg, pr_oid)
        assert_trusted_view(cfg, bump=False)

        write(co / "TauCeti/Shared.lean", "module\n-- repaired PR source\n")
        _, agent_env = tc.agents.host_agent_argv("", "codex")
        agent_env["TAUCETI_REAL_LAKE"] = str(cfg.state / "fake-real-lake")
        run(REPO / "scripts/lake", "build", "TauCeti.Shared", cwd=co, env=agent_env)
        check(
            "trusted source refresh reflects live edits",
            (cfg.state / "trusted-build/TauCeti/Shared.lean").read_text().endswith("repaired PR source\n"),
        )
        check(
            "only the source edit is visible to Git",
            git(co, "status", "--porcelain").stdout == " M TauCeti/Shared.lean\n",
        )
        git(co, "add", "TauCeti/Shared.lean")
        git(co, "commit", "-q", "-m", "fix: repair PR source")
        fixed_oid = git(co, "rev-parse", "HEAD").stdout.strip()
        check("source commit advances the PR branch", fixed_oid != pr_oid)
        check(
            "source commit records the repair",
            contents_at(co, "HEAD", "TauCeti/Shared.lean").endswith("repaired PR source\n"),
        )
        check(
            "source commit retains PR lakefile",
            contents_at(co, "HEAD", "lakefile.toml") == "lakefile = 'old-pr-base'\n",
        )
        check(
            "source commit retains PR manifest",
            contents_at(co, "HEAD", "lake-manifest.json") == '{"revision":"old-pr-base"}\n',
        )
        check("source commit retains PR toolchain", contents_at(co, "HEAD", "lean-toolchain") == "lean-old-pr-base\n")
        check(
            "source commit excludes main-only trusted script",
            git(co, "cat-file", "-e", "HEAD:scripts/main-only.sh", check_rc=False).returncode != 0,
        )

        env = dict(os.environ)
        env.pop("TAUCETI_CLAIM_KEY", None)
        env.update(
            TAUCETI_PUSH_REF="stale-pr",
            TAUCETI_PUSH_EXPECT=pr_oid,
            TAUCETI_PUSH_REMOTE=str(remote),
        )
        pushed = run(REPO / "scripts/git-safe-push", cwd=co, check_rc=False, env=env)
        check("git-safe-push CAS accepts the observed pre-staging PR OID", pushed.returncode == 0)
        check(
            "safe push updates the PR ref to the source-only commit",
            run("git", "--git-dir", remote, "rev-parse", "refs/heads/stale-pr", cwd=co).stdout.strip() == fixed_oid,
        )

        # The next round removes its registered detached view, returns to current main, and retains
        # .lake. A second PR staging proves stale worktree metadata cannot poison later rounds.
        check("next-round prepare_checkout succeeds", tc.agents.prepare_checkout(cfg))
        check("next-round clears trusted build routing", "TAUCETI_TRUSTED_BUILD" not in os.environ)
        check("next-round removes the old detached view", not (cfg.state / "trusted-build").exists())
        check("next-round checkout lands on main", git(co, "branch", "--show-current").stdout.strip() == "main")
        check(
            "next-round checkout restores main config",
            (co / "lakefile.toml").read_text() == "lakefile = 'trusted-main'\n",
        )
        check("next-round checkout restores main source", (co / "TauCeti/MainOnly.lean").is_file())
        check("next-round checkout retains warm .lake", (co / ".lake/warm-current-main").is_file())
        git(co, "switch", "-q", "stale-pr")
        check("second-round trusted staging succeeds", tc.agents.stage_trusted_base_config(cfg))
        assert_trusted_view(cfg, bump=False)
    finally:
        if old_expect is None:
            os.environ.pop("TAUCETI_PUSH_EXPECT", None)
        else:
            os.environ["TAUCETI_PUSH_EXPECT"] = old_expect
        tc.agents._clear_trusted_build(cfg)
        shutil.rmtree(co.parent, ignore_errors=True)


def test_merge_and_rebase_remain_ordinary() -> None:
    for operation in ("merge", "rebase"):
        cfg, _, pr_oid = make_fixture(operation)
        co = cfg.checkout
        try:
            os.environ["TAUCETI_PUSH_EXPECT"] = pr_oid
            check(f"{operation}: trusted staging succeeds", tc.agents.stage_trusted_base_config(cfg))
            if operation == "merge":
                integrated = git(co, "merge", "--no-edit", "origin/main", check_rc=False)
            else:
                integrated = git(co, "rebase", "origin/main", check_rc=False)
            check(f"{operation}: normal Git integration succeeds with trusted view active", integrated.returncode == 0)
            check(f"{operation}: main source is integrated in writable PR", (co / "TauCeti/MainOnly.lean").is_file())
            fake_lake = cfg.state / "integration-fake-lake"
            write(fake_lake, "#!/bin/sh\nexit 0\n")
            fake_lake.chmod(0o755)
            _, agent_env = tc.agents.host_agent_argv("", "codex")
            agent_env["TAUCETI_REAL_LAKE"] = str(fake_lake)
            run(REPO / "scripts/lake", "build", cwd=co, env=agent_env)
            check(
                f"{operation}: next Lake command refreshes integrated source",
                (cfg.state / "trusted-build/TauCeti/MainOnly.lean").is_file(),
            )
            check(
                f"{operation}: build routing preserves original safe-push lease",
                os.environ.get("TAUCETI_PUSH_EXPECT") == pr_oid,
            )
        finally:
            tc.agents._clear_trusted_build(cfg)
            shutil.rmtree(co.parent, ignore_errors=True)


def test_bump_view() -> None:
    cfg, _, pr_oid = make_fixture("bump")
    try:
        check("bump trusted-base staging succeeds", tc.agents.stage_trusted_base_config(cfg, preserve_pr_pins=True))
        assert_pr_worktree_untouched(cfg, pr_oid)
        assert_trusted_view(cfg, bump=True)
    finally:
        tc.agents._clear_trusted_build(cfg)
        shutil.rmtree(cfg.checkout.parent, ignore_errors=True)


def test_source_symlinks_fail_closed() -> None:
    cfg, _, _ = make_fixture("symlink")
    co = cfg.checkout
    try:
        (co / "TauCeti/Nested").mkdir()
        (co / "TauCeti/Nested/Outside.lean").symlink_to(co / "lakefile.toml")
        check("trusted staging rejects nested PR source symlinks", not tc.agents.stage_trusted_base_config(cfg))

        (co / "TauCeti/Nested/Outside.lean").unlink()
        check("trusted staging succeeds after nested symlink removal", tc.agents.stage_trusted_base_config(cfg))
        (co / "TauCeti/Nested/Late.lean").symlink_to(co / "lakefile.toml")
        fake_lake = cfg.state / "symlink-fake-lake"
        write(fake_lake, "#!/bin/sh\nexit 0\n")
        fake_lake.chmod(0o755)
        _, agent_env = tc.agents.host_agent_argv("", "codex")
        agent_env["TAUCETI_REAL_LAKE"] = str(fake_lake)
        routed = run(REPO / "scripts/lake", "build", cwd=co, env=agent_env, check_rc=False)
        check("Lake refresh rejects a symlink introduced after staging", routed.returncode == 2)
        check("Lake refresh identifies the rejected nested symlink", "Nested/Late.lean" in routed.stderr)
    finally:
        tc.agents._clear_trusted_build(cfg)
        shutil.rmtree(cfg.checkout.parent, ignore_errors=True)


class RecordingCounters:
    def __init__(self, events):
        self.events = events
        self.keys = []

    def incr(self, key):
        self.events.append(("counter", key))
        self.keys.append(key)


def exercise_host_fixlike_case(wu, label: str, *, bump_branch: bool = False) -> None:
    events = []
    case = f"{label}-bump-branch" if bump_branch else label
    pr = 731
    surveyed_head = f"{case}-surveyed-head"
    # A bump's green guard is attached to this exact OID, so checkout must not advance it underneath
    # the worker. Ordinary repair branches still exercise the actual-checked-head CAS handoff.
    checked_head = surveyed_head if bump_branch else f"{case}-checked-head"
    claim_key = f"branch/{pr}"
    cfg = SimpleNamespace(checkout=Path("/worker/checkout"), state=Path("/worker/state"), logdir=Path("/worker/logs"))
    counters = RecordingCounters(events)

    class Claims:
        def begin_branch_work(self, got_pr, got_head, refname, owner, repo):
            events.append(("claim", got_pr, got_head, refname, owner, repo))
            os.environ.update(
                TAUCETI_PUSH_REF=refname,
                TAUCETI_PUSH_EXPECT=got_head,
                TAUCETI_PUSH_REMOTE=f"https://github.com/{owner}/{repo}",
                TAUCETI_CLAIM_KEY=claim_key,
            )
            return True

    busted = []
    worker = SimpleNamespace(
        cfg=cfg,
        claims=Claims(),
        counters=counters,
        rs=SimpleNamespace(bust=lambda got_pr: busted.append(got_pr)),
    )
    sv = SimpleNamespace(
        open_prs=[
            SimpleNamespace(
                number=pr,
                head_owner="alice",
                head_repo="TauCeti",
                head_ref="bump-mathlib/validated" if bump_branch else f"repair/{label}",
                bump_guard_success=True,
            )
        ]
    )
    candidate = SimpleNamespace(pr=pr, head=surveyed_head)
    opts = SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True)

    def fake_run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "checkout"]:
            events.append(("checkout", list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:4] == ["git", "-C", str(cfg.checkout), "rev-parse"]:
            events.append(("rev-parse", list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, checked_head + "\n", "")
        raise AssertionError(f"unexpected subprocess: {argv}")

    def fake_stage(got_cfg, *, preserve_pr_pins=False):
        events.append(("stage", got_cfg, preserve_pr_pins))
        return True

    def fake_cache(got_cfg):
        events.append(("cache", got_cfg))

    def fake_agent(co, _prompt, model, logdir):
        events.append(("agent", co, model, logdir, dict(os.environ)))
        return 0

    wu.subprocess.run = fake_run
    wu.stage_trusted_base_config = fake_stage
    wu.fetch_host_lake_caches = fake_cache
    wu.run_agent_host = fake_agent

    call = {"fix": wu.do_fix, "fix-ci": wu.do_fix_ci, "rebase": wu.do_rebase, "bump": wu.do_bump}[label]
    rc = call(worker, sv, candidate, opts, False)
    names = [event[0] for event in events]
    check(f"{case}: host fix path succeeds", rc == 0)
    check(
        f"{case}: claim, checkout, trusted setup, counters, and agent are ordered",
        names.index("claim")
        < names.index("checkout")
        < names.index("stage")
        < names.index("counter")
        < names.index("agent"),
    )
    stage_event = next(event for event in events if event[0] == "stage")
    check(f"{case}: stages via the worker config", stage_event[1] is cfg)
    check(f"{case}: preserve_pr_pins matches CI semantics", stage_event[2] is bump_branch)
    check(f"{case}: bumped cache fetch matches CI timing", ("cache" in names) is bump_branch)
    if bump_branch:
        check(f"{case}: bumped cache fetch precedes counters", names.index("cache") < names.index("counter"))
    agent_env = next(event for event in events if event[0] == "agent")[4]
    check(f"{case}: claim key reaches run_agent_host", agent_env.get("TAUCETI_CLAIM_KEY") == claim_key)
    check(
        f"{case}: checked head reaches agent as CAS expectation", agent_env.get("TAUCETI_PUSH_EXPECT") == checked_head
    )
    expected_keys = {
        "fix": [f"fix-{pr}-{surveyed_head[:12]}"],
        "fix-ci": [f"ci-{pr}-{surveyed_head[:12]}", f"ci-pr-{pr}"],
        "rebase": [f"rebase-pr-{pr}"],
        "bump": [f"bump-{pr}-{surveyed_head[:12]}", f"bump-pr-{pr}"],
    }[label]
    check(f"{case}: semantic counters charge exactly at model launch", counters.keys == expected_keys)
    check(f"{case}: successful host agent busts review state", busted == [pr])


def test_host_fixlike_integration() -> None:
    wu = tc.work_units
    saved = {
        "stage": wu.stage_trusted_base_config,
        "cache": wu.fetch_host_lake_caches,
        "agent": wu.run_agent_host,
        "subprocess_run": wu.subprocess.run,
    }
    push_keys = ("TAUCETI_PUSH_REF", "TAUCETI_PUSH_EXPECT", "TAUCETI_PUSH_REMOTE", "TAUCETI_CLAIM_KEY")
    saved_push_env = {key: os.environ.get(key, _MISSING) for key in push_keys}
    try:
        for label in ("fix", "fix-ci", "rebase"):
            exercise_host_fixlike_case(wu, label)
        exercise_host_fixlike_case(wu, "bump", bump_branch=True)
        # A validated bump can reach another semantic stage (for example a blocking review finding);
        # branch identity, not the dispatcher label, controls pin trust and cache setup.
        exercise_host_fixlike_case(wu, "fix", bump_branch=True)

        # Defense in depth: CI never executes rejected bump pins, even if a stale/manual survey were
        # to hand the work unit an invalid red bump.
        invalid_events = []
        invalid_counters = RecordingCounters(invalid_events)
        invalid_pr = 811
        invalid_worker = SimpleNamespace(
            cfg=SimpleNamespace(checkout=Path("/worker/checkout"), state=Path("/state"), logdir=Path("/logs")),
            counters=invalid_counters,
        )
        invalid_sv = SimpleNamespace(
            open_prs=[
                SimpleNamespace(
                    number=invalid_pr,
                    head_owner="alice",
                    head_repo="TauCeti",
                    head_ref="bump-mathlib/rejected",
                    bump_guard_success=False,
                )
            ]
        )
        invalid_errors = []
        for call in (wu.do_bump, wu.do_fix, wu.do_rebase):
            try:
                call(
                    invalid_worker,
                    invalid_sv,
                    SimpleNamespace(pr=invalid_pr, head="rejected-head"),
                    SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True),
                    False,
                )
                invalid_errors.append(None)
            except Exception as exc:
                invalid_errors.append(exc)
        check(
            "non-green bump-guard blocks every host action",
            all(isinstance(error, tc.NoProgress) for error in invalid_errors),
        )
        check("rejected bump pins charge no semantic counter", invalid_counters.keys == [])

        # A green bump-guard applies to exactly the surveyed head. If checkout races to a newer commit,
        # stop for a fresh survey rather than executing that commit's unvalidated pins.
        race_events = []
        race_counters = RecordingCounters(race_events)
        race_pr = 813
        race_cfg = SimpleNamespace(checkout=Path("/worker/checkout"), state=Path("/worker/state"), logdir=Path("/logs"))
        race_worker = SimpleNamespace(
            cfg=race_cfg,
            counters=race_counters,
            claims=SimpleNamespace(begin_branch_work=lambda *args: True),
            rs=SimpleNamespace(bust=lambda *_: None),
        )
        race_sv = SimpleNamespace(
            open_prs=[
                SimpleNamespace(
                    number=race_pr,
                    head_owner="alice",
                    head_repo="TauCeti",
                    head_ref="bump-mathlib/raced",
                    bump_guard_success=True,
                )
            ]
        )

        def race_run(argv, **_kwargs):
            if argv[:3] == ["gh", "pr", "checkout"]:
                race_events.append(("checkout",))
                return subprocess.CompletedProcess(argv, 0, "", "")
            race_events.append(("rev-parse",))
            return subprocess.CompletedProcess(argv, 0, "new-unvalidated-head\n", "")

        wu.subprocess.run = race_run
        wu.stage_trusted_base_config = lambda *_args, **_kwargs: race_events.append(("stage",)) or True
        wu.fetch_host_lake_caches = lambda *_args: race_events.append(("cache",))
        wu.run_agent_host = lambda *_args, **_kwargs: race_events.append(("agent",)) or 0
        try:
            wu.do_fix(
                race_worker,
                race_sv,
                SimpleNamespace(pr=race_pr, head="guarded-head"),
                SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True),
                False,
            )
            race_error = None
        except Exception as exc:
            race_error = exc
        check("bump head movement forces a fresh survey", isinstance(race_error, tc.NoProgress))
        check("unvalidated raced bump charges no semantic counter", race_counters.keys == [])
        check("unvalidated raced bump creates no trusted view", ("stage",) not in race_events)
        check("unvalidated raced bump launches no agent", ("agent",) not in race_events)

        # A validated bump needs a second cache fetch after its pins are overlaid. That is still
        # machine/cache preparation and must fail before semantic counters or model launch.
        cache_events = []
        cache_counters = RecordingCounters(cache_events)
        cache_pr = 814
        cache_head = "guarded-cache-head"
        cache_worker = SimpleNamespace(
            cfg=race_cfg,
            counters=cache_counters,
            claims=SimpleNamespace(begin_branch_work=lambda *args: True),
            rs=SimpleNamespace(bust=lambda *_: None),
        )
        cache_sv = SimpleNamespace(
            open_prs=[
                SimpleNamespace(
                    number=cache_pr,
                    head_owner="alice",
                    head_repo="TauCeti",
                    head_ref="bump-mathlib/cache-fail",
                    bump_guard_success=True,
                )
            ]
        )

        def exact_head_run(argv, **_kwargs):
            if argv[:3] == ["gh", "pr", "checkout"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, cache_head + "\n", "")

        wu.subprocess.run = exact_head_run
        wu.stage_trusted_base_config = lambda *_args, **_kwargs: cache_events.append(("stage",)) or True

        def fail_bump_cache(*_args):
            cache_events.append(("cache",))
            raise tc.Die("bumped cache unavailable")

        wu.fetch_host_lake_caches = fail_bump_cache
        wu.run_agent_host = lambda *_args, **_kwargs: cache_events.append(("agent",)) or 0
        try:
            wu.do_bump(
                cache_worker,
                cache_sv,
                SimpleNamespace(pr=cache_pr, head=cache_head),
                SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True),
                False,
            )
            cache_error = None
        except Exception as exc:
            cache_error = exc
        check("bump-specific cache failure is fatal", isinstance(cache_error, tc.Die))
        check("bump-specific cache failure follows trusted staging", cache_events[:2] == [("stage",), ("cache",)])
        check("bump-specific cache failure charges no semantic counter", cache_counters.keys == [])
        check("bump-specific cache failure launches no agent", ("agent",) not in cache_events)

        # Checkout failure is PR-specific rather than machine-wide. Charge it so the same broken
        # candidate cannot spin forever while keeping trusted setup/cache failures uncharged.
        checkout_events = []
        checkout_counters = RecordingCounters(checkout_events)
        checkout_pr = 815
        checkout_worker = SimpleNamespace(
            cfg=race_cfg,
            counters=checkout_counters,
            claims=SimpleNamespace(begin_branch_work=lambda *args: True),
            rs=SimpleNamespace(bust=lambda *_: None),
        )
        checkout_sv = SimpleNamespace(
            open_prs=[
                SimpleNamespace(
                    number=checkout_pr,
                    head_owner="alice",
                    head_repo="TauCeti",
                    head_ref="repair/checkout-fail",
                    bump_guard_success=False,
                )
            ]
        )
        wu.subprocess.run = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "checkout failed")
        wu.stage_trusted_base_config = lambda *_args, **_kwargs: checkout_events.append(("stage",)) or True
        wu.run_agent_host = lambda *_args, **_kwargs: checkout_events.append(("agent",)) or 0
        checkout_head = "checkout-fail-head"
        checkout_rc = wu.do_fix_ci(
            checkout_worker,
            checkout_sv,
            SimpleNamespace(pr=checkout_pr, head=checkout_head),
            SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True),
            False,
        )
        check("PR checkout failure returns a failed attempt", checkout_rc == 1)
        check(
            "PR checkout failure charges its semantic attempt",
            checkout_counters.keys == [f"ci-{checkout_pr}-{checkout_head[:12]}", f"ci-pr-{checkout_pr}"],
        )
        check("PR checkout failure performs no trusted staging", ("stage",) not in checkout_events)
        check("PR checkout failure launches no agent", ("agent",) not in checkout_events)

        # A machine-wide trusted-view failure is fatal before either fix-CI counter or model launch.
        events = []
        counters = RecordingCounters(events)
        pr = 812
        cfg = SimpleNamespace(checkout=Path("/worker/checkout"), state=Path("/worker/state"), logdir=Path("/logs"))
        worker = SimpleNamespace(
            cfg=cfg,
            counters=counters,
            claims=SimpleNamespace(begin_branch_work=lambda *args: True),
            rs=SimpleNamespace(bust=lambda *_: None),
        )
        sv = SimpleNamespace(
            open_prs=[
                SimpleNamespace(
                    number=pr,
                    head_owner="alice",
                    head_repo="TauCeti",
                    head_ref="repair/fail",
                    bump_guard_success=True,
                )
            ]
        )
        candidate = SimpleNamespace(pr=pr, head="machine-failure-head")
        opts = SimpleNamespace(agent_name="Codex", work_model="codex", host_prepared=True)
        wu.subprocess.run = lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "machine-failure-head\n" if argv[:2] == ["git", "-C"] else "",
            "",
        )
        wu.stage_trusted_base_config = lambda *_args, **_kwargs: False
        wu.run_agent_host = lambda *_args, **_kwargs: events.append(("agent",)) or 0
        try:
            wu.do_fix_ci(worker, sv, candidate, opts, False)
            error = None
        except Exception as exc:
            error = exc
        check("trusted-view setup failure is fatal", isinstance(error, tc.Die))
        check("trusted-view setup failure charges no semantic counter", counters.keys == [])
        check("trusted-view setup failure launches no agent", ("agent",) not in events)
    finally:
        wu.stage_trusted_base_config = saved["stage"]
        wu.fetch_host_lake_caches = saved["cache"]
        wu.run_agent_host = saved["agent"]
        wu.subprocess.run = saved["subprocess_run"]
        for key, value in saved_push_env.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


saved_git_env = {key: os.environ.get(key, _MISSING) for key in _GIT_ENV_KEYS}
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
try:
    test_normal_view_and_safe_push()
    test_merge_and_rebase_remain_ordinary()
    test_bump_view()
    test_source_symlinks_fail_closed()
    test_host_fixlike_integration()
finally:
    for key, value in saved_git_env.items():
        if value is _MISSING:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
