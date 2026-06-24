"""tauceti — the Tau Ceti worker.

Bare `tauceti` opens a dashboard + launcher; `tauceti work [--loop]` does the work
(one round, or the driver loop); `tauceti status` prints the read-only survey.

The worker acts on FormalFrontier/TauCeti as the authenticated `gh` account, and
treats that account's own PRs as the ones it tends. Each round does exactly ONE unit
of work, chosen in priority order: rebase → review → fix-ci → fix → bump → roadmap.
The `bump` step adapts a red bump-mathlib PR (the review bot opens those; the worker
never authors a bump). Merging, abandoning, and de-duplicating PRs is the repo's CI,
not the worker.

(This module is the CLI entry point; `argparse` shows this docstring as `tauceti --help`.)"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time

from .agents import isolate_home, run_in_bubble
from .config import (
    Config,
    Die,
    NoProgress,
    acquire_slot,
    auto_assign_wid,
    log,
    sanitize_wid,
    set_log_file,
    warn_red,
)
from .constants import AGENTS, ALLOWED_TASKS, EX_NOPROGRESS, TAUCETI, WORK_TASKS
from .github import GitHub
from .loop import cmd_loop, resolve_work_model
from .paths import HERE
from .quota import Quota, _claude_keychain_creds, _safe_exists, claude_dir
from .review_state import ReviewState
from .round import Claims, RoundContext, cmd_heartbeat
from .survey import Counters, survey
from .tui import cmd_tui, render_survey
from .work_units import RoundOpts, Worker, _bubble, run_round, want

# ============================================================================
# CLI
# ============================================================================

WORK_EPILOG = """\
the cascade (priority order; a round does the first that applies):
  rebase    bring our open PRs up to date with their base branch
  review    review an open PR (runs the tauceti-review engine)
  fix-ci    fix red CI on one of our PRs
  fix       address review feedback on one of our PRs
  bump      adapt a red bump-mathlib PR (the worker never authors one)
  roadmap   open a new PR for a roadmap item

  With no --only a round walks the whole cascade; --only pins it to a subset,
  --skip drops a subset (and the two combine by subtraction).

examples:
  tauceti work                          one round: auto agent, in a bubble
  tauceti work --loop                   the driver: keep picking the best job
  tauceti work --loop --only review     a focused reviewer
  tauceti work --loop --skip roadmap    the whole cascade except authoring new PRs
  tauceti work --only roadmap --roadmap-only ReductiveGroups
  tauceti work --loop --roadmap-skip OneParameterSemigroups   leave that area to other workers
  tauceti work --agent claude --host    run Opus directly on the host
  tauceti work --dry-run                show what it WOULD do; act on nothing

multiple workers (share a host, coordinate through GitHub; a distinct id isolates each):
  tauceti work --loop                   auto-assigns worker1, worker2, ... per terminal
  tauceti work --loop --worker-id alice --only review
  tauceti work --loop --worker-id bob   --only roadmap

environment (flags win; see README.md for the full list):
  TAUCETI_AGENT          default for --agent
  TAUCETI_WORKER_ID      pins the worker id (else `work` auto-assigns worker1, worker2, ...)
  TAUCETI_ROADMAP_ONLY   single roadmap area (unset = a fresh random area each round; "" = all areas)
  TAUCETI_ROADMAP_SKIP   comma-separated roadmap areas to exclude from selection
  TAUCETI_QUOTA_CMD      default for --quota-cmd
  TAUCETI_STREAM=1       same as --stream
  CLAUDE_CONFIG_DIR      Claude config/credential dir the pacer and bubble seeding use
                         (account switching, where the creds live in a file)
"""


def add_work_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--loop",
        action="store_true",
        help="run the driver: keep doing rounds (pacing against quota between them) "
        "instead of the default single round",
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="TASKS",
        help="restrict the round to these work units, a comma list of: "
        + ", ".join(ALLOWED_TASKS)
        + " (default: walk the whole cascade)",
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="TASKS",
        help="drop these work units from the round (the inverse of --only); a comma list of "
        "the same names. Combines with --only by subtraction (--only review,fix --skip "
        "fix runs only review)",
    )
    p.add_argument(
        "--agent",
        choices=AGENTS,
        default=None,
        help="which agent to run: auto (Codex preferred, Opus fallback), codex, claude, "
        "or deepseek/minimax (pay-per-token OpenRouter, asked for by name) "
        "(default: $TAUCETI_AGENT or auto)",
    )
    p.add_argument(
        "--host",
        action="store_true",
        help="opt OUT of the bubble sandbox and run the agent directly on the host "
        "(faster, but the agent gets your full credentials and network; default: bubble)",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="stream the agent's (noisy) conversation log to the terminal; "
        "default redirects it to a file under logs/ and prints the path (or $TAUCETI_STREAM=1)",
    )
    p.add_argument(
        "--roadmap-only",
        dest="roadmap_only",
        default=None,
        metavar="AREA",
        help="for roadmap rounds, the single roadmap area to steer toward: a subdirectory of "
        "the TauCetiRoadmap repo. List them by opening the dashboard (bare `tauceti`) and "
        "expanding the roadmap row, or browse github.com/FormalFrontier/TauCetiRoadmap. "
        "Empty string = all areas; omit entirely (and leave $TAUCETI_ROADMAP_ONLY "
        "unset) to pick a fresh random area each round. Overrides "
        "$TAUCETI_ROADMAP_ONLY for this run",
    )
    p.add_argument(
        "--roadmap-skip",
        dest="roadmap_skip",
        default=None,
        metavar="AREA[,AREA...]",
        help="for roadmap rounds, a comma-separated list of roadmap areas to exclude from "
        "selection (so concurrent workers can divide the roadmap) — e.g. "
        "--roadmap-skip OneParameterSemigroups. Excludes them from the auto-random pick and the "
        "all-areas case; --roadmap-only takes precedence if it names a skipped area. Overrides "
        "$TAUCETI_ROADMAP_SKIP for this run",
    )
    p.add_argument(
        "--roadmap-extra-identities",
        dest="roadmap_extra_identities",
        default=None,
        metavar="LOGIN[,LOGIN...]",
        help="additional GitHub logins, beyond your `gh auth` identity, whose registered "
        "intentions this worker should treat as its own (so it won't avoid targets they've "
        "claimed on the intentions board). Comma-separated. Overrides "
        "$TAUCETI_ROADMAP_EXTRA_IDENTITIES for this run",
    )
    p.add_argument(
        "--ignore-claims",
        dest="respect_claims",
        action="store_false",
        default=None,
        help="opt OUT of avoiding roadmap targets other contributors have claimed on the "
        "intentions board (claim-respect is on by default). Sets $TAUCETI_RESPECT_CLAIMS=false",
    )
    p.add_argument(
        "--ignore-quota",
        dest="ignore_quota",
        action="store_true",
        help="ignore the quota PACER (run the requested --agent even when ahead of the burn pace); "
        "a HARD block — a window at 100%%, unreadable usage, or the usage endpoint refusing to answer — "
        "still backs off (needs an explicit --agent codex|claude — 'auto' can't choose without the pacer)",
    )
    p.add_argument(
        "--quota-cmd",
        default=os.environ.get("TAUCETI_QUOTA_CMD"),
        help="external command that decides agent availability, run as '<cmd> <agent>' "
        "(first stdout token = model to run; empty output or nonzero exit = wait); "
        "overrides the built-in pacer (or $TAUCETI_QUOTA_CMD)",
    )
    p.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        metavar="ID",
        help="run an independent worker under this name: it namespaces the worker's state, "
        "checkout, review store, and logs, and (for any id other than 'default') gives it "
        "its own $HOME so credential refreshes don't race. With neither this flag nor "
        "$TAUCETI_WORKER_ID, `work` auto-assigns the lowest free slot (worker1, worker2, "
        "...) so several terminals on one host coexist without hand-numbering",
    )
    p.add_argument(
        "--isolate-home",
        dest="isolate_home",
        action="store_true",
        help="force the per-worker $HOME even for the 'default' worker id (a distinct --worker-id already implies it)",
    )
    p.add_argument(
        "--dry-run", dest="dry_run", action="store_true", help="survey + print the picker's decision; act on nothing"
    )


def parse_only(values: list[str], flag: str = "--only") -> list[str]:
    """Flatten/validate a task flag (comma lists or repeats). Empty = the full cascade."""
    tasks: list[str] = []
    for v in values:
        for tok in v.replace(" ", "").split(","):
            if not tok:
                continue
            if tok not in ALLOWED_TASKS:
                raise SystemExit(
                    f"unknown {flag} task '{tok}' (valid: {', '.join(ALLOWED_TASKS)})"
                )  # exit 1; argparse uses 2
            tasks.append(tok)
    return tasks


def resolve_tasks(only_vals: list[str], skip_vals: list[str]) -> list[str]:
    """Fold --only and --skip into one effective allow-list. With no --skip the --only list passes
    through unchanged (empty ⇒ the whole cascade); --skip materializes the complement, in cascade
    order, so everything downstream — the loop child's argv, want(), the displayed --only — needs to
    know about `only` alone. Combining them subtracts: `--only review,fix --skip fix` ⇒ ['review']."""
    only = parse_only(only_vals, "--only")
    skip = set(parse_only(skip_vals, "--skip"))
    if not skip:
        return only
    allowed = set(only) if only else set(ALLOWED_TASKS)
    tasks = [t for t in ALLOWED_TASKS if t in allowed and t not in skip]
    if not tasks:
        raise SystemExit("--only/--skip leave no work units enabled")
    return tasks


def resolve_agent(args) -> str:
    return getattr(args, "agent", None) or os.environ.get("TAUCETI_AGENT") or "auto"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tauceti",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run 'tauceti work -h' for the work units, examples, multi-worker setup, and\n"
        "environment variables. See README.md for the full reference.",
    )
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser(
        "work",
        help="do work: one round, or the driver loop with --loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Do one unit of work and exit, or pass --loop to run the driver. A round picks the\n"
        "first applicable job from the cascade (or from --only), paces subscription agents\n"
        "against your quota, and runs it on --agent inside a bubble sandbox (or --host).",
        epilog=WORK_EPILOG,
    )
    add_work_flags(w)

    s = sub.add_parser("status", help="read-only survey of available work + quota")
    s.add_argument("--json", action="store_true", help="emit the survey as JSON")
    s.add_argument("--worker-id", dest="worker_id", default=None)

    sub.add_parser("doctor", help="check the environment (tools, bubble, quota creds)")

    # Hidden internal subcommands.
    r = sub.add_parser("_round", add_help=False)
    add_work_flags(r)
    hb = sub.add_parser("_heartbeat", add_help=False)
    hb.add_argument("key")
    hb.add_argument("--ppipe", type=int, default=None)
    ep = sub.add_parser("_egress-probe", add_help=False)
    ep.add_argument("--worker-id", dest="worker_id", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd

    if cmd is None:
        return cmd_tui(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd in ("work", "_round"):
        only = resolve_tasks(getattr(args, "only", []), getattr(args, "skip", []))
        agent = resolve_agent(args)
        return cmd_work(args, only=only, agent=agent, one_round=(cmd == "_round"))
    if cmd == "doctor":
        return cmd_doctor(args)
    if cmd == "_heartbeat":
        return cmd_heartbeat(args)
    if cmd == "_egress-probe":
        return cmd_egress_probe(args)
    parser.print_help()
    return 64


def cmd_status(args) -> int:
    cfg = Config.resolve(getattr(args, "worker_id", None))
    gh = GitHub()
    rs = ReviewState(cfg, gh)
    counters = Counters(cfg)
    sv = survey(cfg, gh, rs, counters, deep=True)
    _, quota_snap = Quota(cfg).choose(None)

    if getattr(args, "json", False):
        out = {"survey": dataclasses.asdict(sv), "quota": {k: dataclasses.asdict(v) for k, v in quota_snap.items()}}
        print(json.dumps(out, indent=2, default=str))
        return 1 if sv.github_failed else 0

    from rich.console import Console

    render_survey(sv, Console(), quota_snap)
    return 1 if sv.github_failed else 0


def cmd_work(args, *, only: list[str], agent: str, one_round: bool) -> int:
    # --roadmap-only overrides the env for this run (and is inherited by loop children, which read it
    # live via roadmap_only()). Empty string is a meaningful value: "all areas".
    if getattr(args, "roadmap_only", None) is not None:
        os.environ["TAUCETI_ROADMAP_ONLY"] = args.roadmap_only
    # --roadmap-skip likewise overrides the env and is inherited by loop children (read live via
    # roadmap_skip()).
    if getattr(args, "roadmap_skip", None) is not None:
        os.environ["TAUCETI_ROADMAP_SKIP"] = args.roadmap_skip
    # --roadmap-extra-identities and --ignore-claims override the env and are inherited by loop
    # children (read live via roadmap_extra_identities() / respect_claims()).
    if getattr(args, "roadmap_extra_identities", None) is not None:
        os.environ["TAUCETI_ROADMAP_EXTRA_IDENTITIES"] = args.roadmap_extra_identities
    if getattr(args, "respect_claims", None) is False:
        os.environ["TAUCETI_RESPECT_CLAIMS"] = "false"
    # --stream restores live agent output (default redirects it to a log file). Set in the env so loop
    # children inherit it.
    if getattr(args, "stream", False):
        os.environ["TAUCETI_STREAM"] = "1"
    # Resolve the worker id. An explicit --worker-id (or $TAUCETI_WORKER_ID, which loop children inherit)
    # pins it; with neither, auto-assign the lowest free slot (worker1, worker2, ...) so several
    # `work --loop` terminals on one host don't collide without the operator hand-numbering them. Pinning
    # the chosen id in the env makes Config.resolve and loop children agree on it.
    explicit = getattr(args, "worker_id", None) or os.environ.get("TAUCETI_WORKER_ID")
    is_loop_driver = getattr(args, "loop", False) and not one_round
    if explicit:
        wid = sanitize_wid(explicit)
        # A top-level loop driver also reserves its slot, so an auto-assigned peer in another terminal
        # can't pick the same id (e.g. you restart `--worker-id worker2` while others auto-assign). The
        # _round children (which inherit this id) and one-shot rounds skip this; they arbitrate via the
        # per-round round.lock instead, so reserving here would only make a child collide with its parent.
        if is_loop_driver and not acquire_slot(wid):
            warn_red(
                f"another loop already holds worker slot '{wid}'; they will contend on its state, "
                f"$HOME, and quota pacing — use a distinct --worker-id"
            )
    else:
        wid = auto_assign_wid()
        os.environ["TAUCETI_WORKER_ID"] = wid
        log(f"auto-assigned worker slot {wid} (override with --worker-id)")
    # A distinct worker id implies its own $HOME (so several workers' credential refreshes don't race);
    # --isolate-home forces it even for the 'default' id. This must set $HOME BEFORE Config.resolve, so
    # cfg.home + credential paths point at the per-worker copy. isolate_home is a no-op when $HOME is
    # already the worker home, so loop children that inherit it don't re-isolate.
    if wid != "default" or getattr(args, "isolate_home", False):
        isolate_home(wid)
    cfg = Config.resolve(wid)
    # Tee this process's log() output to a per-worker file so a `work --loop` started in a bare terminal
    # still leaves an on-disk record to monitor. The loop driver picks one session file and exports its
    # path; the _round children it spawns inherit TAUCETI_LOG_FILE and append to the SAME file.
    set_log_file(cfg.logdir)
    if getattr(args, "loop", False) and not one_round:
        return cmd_loop(args, cfg, only=only, agent=agent)
    dry = getattr(args, "dry_run", False)
    ignore_quota = getattr(args, "ignore_quota", False)
    quota_cmd = getattr(args, "quota_cmd", None)

    with RoundContext(cfg) as ctx:  # one round per worker; cleanup + SIGTERM/SIGINT handling
        # Lifecycle test hooks (no GitHub, no model, no mutation) — exercise lock / fd-leak / timeout.
        hb = os.environ.get("TAUCETI_TEST_HEARTBEAT")
        if hb:
            Claims(cfg, ctx).start_heartbeat("branch/test")
            log(f"[test] heartbeat started; holding {hb}s then exiting")
            time.sleep(int(hb))
            return 0
        hold = os.environ.get("TAUCETI_TEST_HOLD")
        if hold:
            subprocess.Popen(["sleep", hold])  # grandchild; close_fds=True ⇒ must NOT inherit the lock fd
            log(f"[test] spawned 'sleep {hold}' grandchild, round exiting immediately")
            return 0
        slp = os.environ.get("TAUCETI_TEST_SLEEP")
        if slp:
            log(f"[test] holding the lock and sleeping {slp}s")
            time.sleep(int(slp))
            return 0

        work_model = resolve_work_model(cfg, agent, dry=dry, ignore_quota=ignore_quota, quota_cmd=quota_cmd)
        gh = GitHub()
        w = Worker(cfg, gh, ReviewState(cfg, gh), Counters(cfg), ctx, Claims(cfg, ctx))
        opts = RoundOpts(
            only=only,
            agent=agent,
            work_model=work_model,
            sandbox_host=getattr(args, "host", False),
            dry_run=dry,
            ignore_quota=ignore_quota,
            quota_cmd=quota_cmd,
        )
        if not dry:
            preflight(cfg, opts)
        return run_round(w, opts)  # NoProgress/Die propagate to main()'s handler


def cmd_egress_probe(args) -> int:
    """Open a review-posture bubble (same flags as review_in_bubble) and assert the network boundary:
    the TauCeti-scoped proxy is reachable but arbitrary egress is denied. This is the property that makes
    a tool-using reviewer safe — re-run it whenever the bubble image / security policy changes, and once
    review gains tool use. Prints PROXY_OK + EGRESS_BLOCKED on success; tests/egress.sh asserts both."""
    cfg = Config.resolve(getattr(args, "worker_id", None))
    # set +e so a non-zero curl (the blocked case) doesn't abort the script before we print the verdict.
    probe = (
        "sh -lc 'set +e; "
        "if gh auth token >/dev/null 2>&1 && gh api /repos/FormalFrontier/TauCeti >/dev/null 2>&1; "
        "then echo PROXY_OK; else echo PROXY_FAIL; fi; "
        "if curl -sS --max-time 10 -o /dev/null https://example.com 2>/dev/null; "
        "then echo EGRESS_LEAK; else echo EGRESS_BLOCKED; fi'"
    )
    with RoundContext(cfg) as ctx:
        gh = GitHub()
        w = Worker(cfg, gh, ReviewState(cfg, gh), Counters(cfg), ctx, Claims(cfg, ctx))
        opts = RoundOpts(only=["review"], agent="claude", work_model="claude", sandbox_host=False, dry_run=False)
        # No mounts; cred_model=claude just satisfies bubble's credential flags (the probe runs no model).
        return run_in_bubble(w, TAUCETI, "", opts, inner_cmd=probe, cred_model="claude")


def _have(tool: str) -> bool:
    import shutil

    return shutil.which(tool) is not None


def cmd_doctor(args) -> int:
    """Report what the environment can do. bubble and the review engine are fetched on demand, so they
    are informational, not required."""
    cfg = Config.resolve(getattr(args, "worker_id", None))
    rows: list[tuple[str, bool, str]] = []
    rows.append(("gh", _have("gh"), "required"))
    rows.append(("git", _have("git"), "required"))
    rows.append(("uv/uvx", _have("uvx"), "required (runs tauceti, fetches bubble + review engine)"))
    rows.append(("jq", _have("jq"), "claim.sh needs it"))
    gh_auth = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    rows.append(("gh auth", gh_auth, "the worker acts as this account; its PRs are the ones it tends"))
    rows.append(("bubble", _have("bubble"), "default sandbox (fetched on demand if absent)"))
    rows.append(("incus", _have("incus"), "bubble's container runtime — install it, or use --host"))
    rows.append(("lake", _have("lake"), "only --host authoring builds with it"))
    rows.append(("pi", _have("pi"), "for --agent deepseek/minimax"))
    rows.append(("codex creds", _safe_exists(cfg.home / ".codex" / "auth.json"), "~/.codex/auth.json"))
    claude_creds = claude_dir(cfg.home) / ".credentials.json"
    if _safe_exists(claude_creds) or sys.platform != "darwin":
        rows.append(("claude creds", _safe_exists(claude_creds), str(claude_creds)))
    else:
        # macOS keeps Claude creds in the login Keychain, not a file; the pacer reads them there.
        rows.append(
            ("claude creds", _claude_keychain_creds() is not None, 'macOS login Keychain ("Claude Code-credentials")')
        )
    bad = 0
    print(f"tauceti doctor — worker '{cfg.wid}'")
    for name, ok, note in rows:
        mark = "ok " if ok else "MISSING"
        if not ok and name in ("gh", "git", "uv/uvx", "gh auth"):
            bad += 1
        print(f"  [{mark:7}] {name:14} {note}")
    return 1 if bad else 0


def preflight(cfg: Config, opts: RoundOpts) -> None:
    """Fail a round early if its chosen mode can't run, rather than partway through. bubble and the
    review engine are fetched on demand, so we don't require them on PATH (only uvx + a runtime)."""
    for t in ("gh", "git", "uvx"):
        if not _have(t):
            raise Die(f"preflight: missing '{t}' on PATH")
    needs_host_build = any((not _bubble(s, opts)) for s in WORK_TASKS if want(opts.only, s))
    if needs_host_build and not _have("lake") and not opts.dry_run:
        raise Die("preflight: --host authoring needs an elan/lake toolchain on PATH (or drop --host)")
    # bubble (the default sandbox) runs each model on untrusted PR content inside an Incus container.
    # Without Incus, bubble fails deep in the round with a terse "Incus is required but not installed";
    # catch it here with a pointer to the two ways out.
    uses_bubble = any(_bubble(s, opts) for s in WORK_TASKS if want(opts.only, s))
    if uses_bubble and not opts.dry_run and not _have("incus"):
        raise Die(
            "preflight: the bubble sandbox needs a working Incus runtime, but `incus` is not on PATH.\n"
            "  bubble runs each model on untrusted PR content inside an Incus container. Either:\n"
            "    - install Incus (https://linuxcontainers.org/incus/), then re-run; or\n"
            "    - re-run with --host to skip the sandbox and run on this host directly (the agent then\n"
            "      has your full gh credentials and network, so use it only on trusted/disposable machines).\n"
            "  `tauceti doctor` reports this too."
        )


def cli_main() -> int:
    """Console-script entry point (also used by ./tauceti). Maps the worker's exceptions to exit codes."""
    _ensure_scripts_executable()
    try:
        return main() or 0
    except Die as e:
        log(str(e))
        return 1
    except NoProgress as e:
        log(str(e))
        return EX_NOPROGRESS
    except KeyboardInterrupt:
        return 130


def _ensure_scripts_executable() -> None:
    """A wheel install drops the execute bit on the bundled scripts/ wrappers; restore it so the agents
    can run git-safe-push / gh-safe-pr-create / claim.sh on PATH. Cheap and idempotent."""
    for f in ("claim.sh", "git-safe-push", "gh-safe-pr-create"):
        p = HERE / "scripts" / f
        try:
            if p.exists() and not os.access(p, os.X_OK):
                p.chmod(0o755)
        except OSError:
            pass
