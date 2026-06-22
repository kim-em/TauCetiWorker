"""tauceti_worker.work_units — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .agents import (
    fetch_ref,
    fill_prompt,
    prepare_checkout,
    review_in_bubble,
    run_agent_host,
    run_in_bubble,
    run_to_logfile,
)
from .config import Config, Die, NoProgress, log, roadmap_areas, roadmap_skip, warn_red
from .constants import (
    AGENT_NAMES,
    CONTEST_CLAIM_TTL,
    MAX_OPEN_PRS,
    REVIEW,
    REVIEW_DAILY_CAP,
    ROADMAP,
    SANDBOX_DEFAULT,
    TAUCETI,
)
from .github import GitHub, GitHubError, gh_run, me
from .paths import HERE
from .review_state import ReviewState
from .round import Claims, RoundContext
from .survey import TARGET_MARKER_RE, Candidate, Counters, Survey, spread_candidates, survey

# ============================================================================
# Round — the want-gated cascade over survey(). Pre-passes (merge/abandon/dedup)
# always run (quota-free); then ONE work unit. Mirrors round.sh main().
# ============================================================================


def want(only: list[str], task: str) -> bool:
    """Is this work-unit stage enabled? Empty `only` ⇒ everything (do-whatever-is-helpful)."""
    return (not only) or (task in only)


@dataclass
class RoundOpts:
    only: list[str]
    agent: str  # auto|codex|claude|deepseek|minimax (the requested dial)
    work_model: str  # the concrete model to run (codex|claude|deepseek|minimax), or 'auto' for dry-run
    sandbox_host: bool  # True = --host (opt out of bubble)
    dry_run: bool
    ignore_quota: bool = False
    quota_cmd: str | None = None

    @property
    def agent_name(self) -> str:
        return AGENT_NAMES.get(self.work_model, self.work_model)


@dataclass
class Worker:
    cfg: Config
    gh: GitHub
    rs: ReviewState
    counters: Counters
    rc: RoundContext
    claims: Claims


def _bubble(stage: str, opts: RoundOpts) -> bool:
    """True = run this stage in bubble. Every model-running stage defaults to bubble; --host opts out."""
    if not SANDBOX_DEFAULT.get(stage, False):
        return False
    return not opts.sandbox_host


def run_round(w: Worker, opts: RoundOpts) -> int:
    sv = survey(w.cfg, w.gh, w.rs, w.counters, deep=True)
    if sv.github_failed:
        raise NoProgress("gh pr list failed (GitHub API?) — aborting round, not falling through to authoring")

    log(f"open PRs: {sv.n_open_nondraft} non-draft, {sv.n_reviewable} build-green")
    for pr, providers in sv.review_inflight:
        log(f"  review #{pr}: a peer reviewer ({providers}) holds this head — skipping (no duplicate spend)")
    for pr, count in sv.review_capped:
        if count.startswith("?"):
            log(f"  review #{pr}: local ledger unreadable — skipping review (fail-closed); fix the ledger")
        else:
            log(f"  review #{pr}: daily cap {count} reached — skipping until 00:00 UTC (no launch/clone)")

    # Escalate every PR the worker can't review (its review keeps erroring). This fires EVERY round
    # the condition holds — a bright-red warning so it can't be missed — and ensures one tracking issue
    # per PR for a permanent record. These PRs neither merge nor advance toward CI's round cap, so a
    # human must intervene; surfacing them loudly is the alternative to stranding them in silence.
    for pr in sv.review_stuck:
        n_err = w.counters.read(f"review-err-{pr}")
        warn_red(
            f"PR #{pr}: review has ERRORED {n_err}x without posting a verdict — the worker cannot "
            f"review it. Needs a human. https://github.com/{TAUCETI}/pull/{pr}"
        )
        w.gh.ensure_stuck_issue(pr, f"its review has errored {n_err} times without posting a verdict")

    # Spread concurrent workers across different PRs: shuffle each CONTENDED stage's candidates so workers
    # starting together don't all pick the lowest-numbered PR and probe the same target in lockstep
    # (review collides on the in-progress marker; fix/fix-ci/rebase each cost a branch-claim round-trip to
    # discover the clash). This only reorders WITHIN a stage — the cascade's stage priority below is
    # unchanged — and the real de-contention (marker / branch claim) remains the authority and backstop.
    for stage in ("rebase", "review", "fix-ci", "fix", "bump"):
        sv.kind(stage).actionable = spread_candidates(sv.kind(stage).actionable)

    # The cascade: first actionable stage wins, does ONE unit, returns its rc. A candidate whose branch
    # is claimed by another worker is skipped to the next candidate in the same stage (COOP dedup).
    # fix-ci before fix: a red PR can't be reviewed or review-fixed until it builds. bump adapts a
    # bump-mathlib PR (opened by the review bot) that mathlib moved out from under.
    for stage in ("rebase", "review", "fix-ci", "fix", "bump"):
        if not want(opts.only, stage):
            continue
        for c in sv.kind(stage).actionable:
            rc = dispatch(stage, w, sv, c, opts)
            if rc is not None:
                return rc  # performed (or dry-run); else (None) claimed-elsewhere → try next candidate
    if want(opts.only, "roadmap"):
        if sv.roadmap_backpressure:
            raise NoProgress(f"roadmap: {sv.n_mine_open} open PRs (>= {MAX_OPEN_PRS}) — backpressure, not authoring")
        rc = dispatch("roadmap", w, sv, Candidate(0, "", sv.roadmap_only), opts)
        if rc is not None:
            return rc

    raise NoProgress(f"no eligible work this round under --only={','.join(opts.only) or '(all)'}")


# Authoring/fixing stages whose success MUST leave a mark on GitHub (a push, a new PR, or — for a
# contested fix — a comment). `review` is excluded: it posts a scoreboard and its rc is the engine's.
PROGRESS_GUARDED = {"rebase", "fix", "fix-ci", "bump", "roadmap"}


def _open_pr_numbers(w: Worker) -> set[int] | None:
    try:
        return {p["number"] for p in w.gh.pr_list(["number"], state="open")}
    except GitHubError:
        return None


def _progress_snapshot(w: Worker, c: Candidate) -> dict | None:
    """Capture just enough GitHub state to tell, after the round, whether the agent actually changed
    anything. Returns None if we can't snapshot — then the guard is skipped (never block a real
    success on a flaky query)."""
    if c.pr:
        st = w.gh.pr_progress_state(c.pr)  # head + comment count in one GraphQL call
        if st is None:
            return None
        return {"head": st["head"] or c.head, "ncomments": st["ncomments"]}
    nums = _open_pr_numbers(w)  # roadmap / bump: a new marker-bearing PR = progress
    return {"prs": nums} if nums is not None else None


def _progressed(w: Worker, c: Candidate, pre: dict | None) -> bool:
    """True if the round left an observable mark (push / new PR / new issue-or-review comment).
    Conservative: any query failure or ambiguity returns True, so we never falsely discard real work."""
    if pre is None:
        return True
    if c.pr:
        st = w.gh.pr_progress_state(c.pr)
        if st is None:
            return True
        return (st["head"] or "") != pre["head"] or st["ncomments"] > pre["ncomments"]
    now = _open_pr_numbers(w)
    if now is None:
        return True
    new = now - pre["prs"]
    if not new:
        return False
    # A new PR appeared — but only one carrying a tauceti-target marker is THIS round's authoring work.
    # An unrelated/human PR (or, under multi-worker, another worker's concurrent PR) that shows up
    # mid-round must not mask this round's no-op. Conservative: if we can't read a body, assume ours.
    for num in new:
        v = w.gh.pr_view(num, ["body"])
        if v is None:
            return True
        if TARGET_MARKER_RE.search(v.get("body") or ""):
            return True
    return False


def dispatch(stage: str, w: Worker, sv: Survey, c: Candidate, opts: RoundOpts) -> int | None:
    """Perform one stage. Returns its rc, or None if the candidate was claimed by another worker
    (caller tries the next candidate). Dry-run logs the intent and returns 0."""
    bubble = _bubble(stage, opts)
    if opts.dry_run:
        target = f"#{c.pr}" if c.pr else (c.head[:12] if c.head else c.reason)
        log(
            f"[dry-run] would {stage.upper()} {target}  agent={opts.work_model} "
            f"sandbox={'bubble' if bubble else 'host'}"
        )
        return 0
    fn = {
        "review": do_review,
        "fix": do_fix,
        "fix-ci": do_fix_ci,
        "rebase": do_rebase,
        "bump": do_bump,
        "roadmap": do_roadmap,
    }[stage]
    # Announce the round up front so the log says what was chosen, on which PR (as a clickable URL),
    # with which agent and sandbox — the same line for every stage.
    where = "bubble" if bubble else "host"
    if c.pr:
        what = f"PR #{c.pr}  https://github.com/{TAUCETI}/pull/{c.pr}"
    elif stage == "roadmap":
        what = f"new PR (area: {c.reason or 'any'})"
    else:
        what = c.reason or (c.head[:12] if c.head else "")
    log(f"→ {stage.upper()}: {what}   [agent={opts.work_model}, sandbox={where}]")
    pre = _progress_snapshot(w, c) if stage in PROGRESS_GUARDED else None
    rc = fn(w, sv, c, opts, bubble)
    # A model round that exits 0 but leaves no mark on GitHub did no real work. Usually benign: another
    # worker pushed the branch first and safe-push declined rather than clobber, or the agent chose not
    # to act. Surface it as no-progress (so the loop backs off) but say so plainly and point at the log.
    if rc == 0 and stage in PROGRESS_GUARDED and not _progressed(w, c, pre):
        tgt = f" #{c.pr}" if c.pr else ""
        raise NoProgress(
            f"{stage}{tgt}: the agent finished but nothing landed on GitHub (no push, new PR, or "
            f"comment). Most often another worker pushed the branch first (safe-push declines rather "
            f"than clobber) or the agent declined to act — not a failure. Transcript: {w.cfg.logdir}"
        )
    return rc


# --- the work units (host path here; bubble path lands in M9) ---------------


def do_review(w: Worker, sv: Survey, c: Candidate, opts: RoundOpts, bubble: bool) -> int:
    pr, head = c.pr, c.head
    reviewers = opts.work_model
    if reviewers in ("auto", ""):
        raise Die("review needs a concrete reviewer model (resolve --agent / quota first)")
    errkey = f"review-err-{pr}"
    if c.contest:
        # Claim the in-flight contest with a 👀 on the contesting reply so a peer worker re-surveying
        # before the new scoreboard lands skips it (cross-fleet dedup). The engine auto-detects the
        # contest from the thread reply (no extra flag); a contest-only round is recorded as a reply
        # round, so it does not consume the review-round budget.
        if c.contest_reply_id and not w.gh.add_reaction(c.contest_reply_id):
            log(f"  review #{pr}: contest claim (👀) failed to post — a peer may double-review")
        log(f"  review #{pr}: author contest on {c.contest} @ {head[:12]}, reviewers={reviewers}")
    else:
        nrnd = w.rs.review_rounds(pr, w.counters)
        log(f"  review round {nrnd + 1} @ {head[:12]}, reviewers={reviewers} (CI retires at the cap)")
    try:
        if bubble:
            rc = review_in_bubble(w, pr, head, reviewers, opts)  # M9b
        else:
            logf = w.cfg.logdir / f"review-{pr}-{time.strftime('%Y%m%d-%H%M%S')}.log"
            rc = run_to_logfile(
                [
                    "uvx",
                    "--from",
                    f"git+https://github.com/{REVIEW}",
                    "tauceti-review",
                    str(pr),
                    "--store",
                    str(w.cfg.store_dir),
                    "--post",
                    "--no-sync",
                    "--reviewer",
                    reviewers,
                    "--expect-head",
                    head,
                    "--max-rounds-per-day",
                    str(REVIEW_DAILY_CAP),
                    "--submitted-by",
                    me(),
                ],
                logf,
                f"review #{pr}",
            )
        log(f"  review #{pr}: engine rc={rc}")
        if rc == 0:
            # The engine archived this round's records to <store>/outbox but did NOT push (--no-sync).
            # Publish them to TauCetiData with the host's creds. Loud on failure: records stuck in the
            # outbox mean the merge gate can't see this round, so don't report the round as a success.
            srv = _sync_review_outbox(w, pr)
            if srv != 0:
                log(
                    f"  review #{pr}: archived but the TauCetiData sync FAILED — records remain in "
                    f"{w.cfg.store_dir / 'outbox'} (the merge gate can't see this round until they "
                    f"land); a later round retries the drain"
                )
                w.counters.incr(errkey)
                return srv
            w.counters.write(errkey, 0)
            if c.contest:
                # The engine advanced replies_through in the new scoreboard (the durable per-reply
                # watermark); rs.bust below re-fetches it, so this contest won't re-fire once the 👀
                # is dropped. Just bump the contest caps.
                w.counters.incr(f"review-contest-{pr}")
                w.counters.incr(f"review-contest-{pr}-{c.contest}")
            w.rs.bust(pr)
        else:
            w.counters.incr(errkey)
        return rc
    finally:
        # Drop the claim: on success the watermark now prevents a re-fire; on failure releasing it lets
        # the contest be retried. A crash before here leaves the 👀 to TTL out (CONTEST_CLAIM_TTL).
        if c.contest and c.contest_reply_id and not w.gh.remove_reaction(c.contest_reply_id):
            log(f"  review #{pr}: contest claim (👀) failed to release — it will TTL out in {CONTEST_CLAIM_TTL // 60}m")


def _sync_review_outbox(w: Worker, pr: int) -> int:
    """Drain the worker's review outbox into TauCetiData using the host's gh/git creds. Reviews run
    with --no-sync (a bubble can't push to TauCetiData), so the host publishes here. Returns the
    engine rc: nonzero means the push failed after archive.sync's retries (the outbox is preserved
    write-if-absent, so a later round re-drains it). An empty outbox is a no-op — a round that
    produced no new records is not a publish failure."""
    outbox = w.cfg.store_dir / "outbox"
    if not outbox.is_dir() or not any(p.is_file() for p in outbox.rglob("*")):
        return 0
    # A contributor without write access to TauCetiData (anyone but the maintainer/worker identity)
    # cannot push records there. Don't fail their round over it: the review IS posted and the records
    # are kept in the local outbox — an external review will count once contributor-publishing lands.
    # The maintainer's identity returns push=true, so the sync below runs and a genuine outage still
    # surfaces loudly. A failed/ambiguous check falls through to the sync (preserving the loud-fail).
    perm = gh_run(["gh", "api", "repos/FormalFrontier/TauCetiData", "--jq", ".permissions.push"])
    if perm.returncode == 0 and perm.stdout.strip() == "false":
        log(
            f"  review #{pr}: no write access to TauCetiData — review posted, records kept in "
            f"{outbox} (they won't count for auto-merge until contributor-publishing lands)"
        )
        return 0
    eng = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")  # a local engine checkout, for pre-merge tests
    if eng:
        argv = [
            sys.executable,
            str(Path(eng) / "runner" / "cli.py"),
            str(pr),
            "--sync-only",
            "--store",
            str(w.cfg.store_dir),
        ]
    else:
        argv = [
            "uvx",
            "--from",
            f"git+https://github.com/{REVIEW}",
            "tauceti-review",
            str(pr),
            "--sync-only",
            "--store",
            str(w.cfg.store_dir),
        ]
    # The sync echoes a full `$ …python …/archive.py sync --store … --data-dir …` command line and a
    # "synced N file(s)" line. Capture it so that noise stays out of the main log, surfacing only a
    # one-line summary; keep the detail in a subsidiary file only when the sync FAILS (the diagnosable case).
    if os.environ.get("TAUCETI_STREAM"):
        return subprocess.run(argv).returncode
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode == 0:
        m = re.search(r"synced (\d+) file", (p.stdout or "") + (p.stderr or ""))
        log(f"  review #{pr}: synced {m.group(1) if m else '?'} record(s) to TauCetiData")
    else:
        logf = w.cfg.logdir / f"sync-{pr}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        try:
            w.cfg.logdir.mkdir(parents=True, exist_ok=True)
            logf.write_text((p.stdout or "") + (p.stderr or ""))
            log(f"  review #{pr}: TauCetiData sync FAILED (rc={p.returncode}); detail → {logf}")
        except OSError:
            log(f"  review #{pr}: TauCetiData sync FAILED (rc={p.returncode})")
    return p.returncode


def _do_fixlike(
    w: Worker, sv: Survey, c: Candidate, opts: RoundOpts, bubble: bool, *, prompt_file: str, label: str
) -> int | None:
    """Shared shape for fix / fix-ci / rebase: take the branch claim, then run the agent against the PR
    branch — in bubble (it checks out the PR inside the container) or on the host checkout."""
    pr, head = c.pr, c.head
    p = next((x for x in sv.open_prs if x.number == pr), None)
    if p is None:
        raise Die(f"{label}: PR #{pr} vanished from the survey")
    if not w.claims.begin_branch_work(pr, head, p.head_ref, p.head_owner, p.head_repo):
        return None  # claimed elsewhere → caller tries the next candidate
    prompt = fill_prompt(HERE / "prompts" / prompt_file, PR=pr, AGENT=opts.agent_name)
    if bubble:
        rc = run_in_bubble(w, f"{TAUCETI}/pull/{pr}", prompt, opts)  # bubble checks out the PR inside
    else:
        if not prepare_checkout(w.cfg):
            log(f"checkout failed for #{pr} — skipping this attempt")
            return 1
        co = w.cfg.checkout
        # Capture the checkout's git chatter ("Switched to a new branch …", "set up to track …") instead
        # of letting it spill into the main log; surface a one-line summary, and the stderr only on failure.
        chk = subprocess.run(["gh", "pr", "checkout", str(pr), "--force"], cwd=str(co), capture_output=True, text=True)
        if chk.returncode:
            detail = ((chk.stderr or "") + (chk.stdout or "")).strip()[-200:]
            log(f"  {label} #{pr}: gh pr checkout failed — skipping this attempt ({detail})")
            return 1
        rev = subprocess.run(["git", "-C", str(co), "rev-parse", "HEAD"], capture_output=True, text=True)
        checked = rev.stdout.strip() or head
        os.environ["TAUCETI_PUSH_EXPECT"] = checked  # CAS against what we actually checked out
        log(f"  {label} #{pr}: checked out @ {checked[:12]}")
        rc = run_agent_host(co, prompt, opts.work_model, w.cfg.logdir)
    if rc == 0:
        w.rs.bust(pr)
    return rc


def do_fix(w, sv, c, opts, bubble) -> int | None:
    pr, head = c.pr, c.head
    w.counters.incr(f"fix-{pr}-{head[:12]}")  # count up front (an un-checkout-able PR mustn't loop)
    return _do_fixlike(w, sv, c, opts, bubble, prompt_file="fix.md", label="fix")


def do_fix_ci(w, sv, c, opts, bubble) -> int | None:
    pr, head = c.pr, c.head
    w.counters.incr(f"ci-{pr}-{head[:12]}")
    w.counters.incr(f"ci-pr-{pr}")
    return _do_fixlike(w, sv, c, opts, bubble, prompt_file="fix-ci.md", label="fix-ci")


def do_rebase(w, sv, c, opts, bubble) -> int | None:
    w.counters.incr(f"rebase-pr-{c.pr}")
    return _do_fixlike(w, sv, c, opts, bubble, prompt_file="rebase.md", label="rebase")


def do_bump(w, sv, c, opts, bubble) -> int | None:
    """Adapt a red bump-mathlib PR (the bot bumped mathlib; TauCeti/ needs to catch up). Same
    shape as a fix: claim the branch, check the PR out, drive the agent on prompts/bump.md to green it."""
    pr, head = c.pr, c.head
    w.counters.incr(f"bump-{pr}-{head[:12]}")  # count up front so an un-checkout-able PR can't loop
    w.counters.incr(f"bump-pr-{pr}")
    return _do_fixlike(w, sv, c, opts, bubble, prompt_file="bump.md", label="bump")


def do_roadmap(w, sv, c, opts, bubble) -> int:
    only = c.reason or "any"
    skip = roadmap_skip()
    if only == "auto":  # no area pinned: pick a fresh random area this round (per-round, in-child)
        areas = [a for a in roadmap_areas(w.gh) if a not in skip]
        only = random.choice(areas) if areas else "any"
        log(f"→ ROADMAP area: {only} (auto-picked from {len(areas)} areas, skipping {len(skip)})")
    elif only not in ("any", "") and only in skip:  # --roadmap-only wins over an overlapping skip
        log(f"→ ROADMAP area: {only} (--roadmap-only overrides --roadmap-skip)")
    skip_str = ", ".join(skip) or "none"
    refs = w.cfg.state / "refs"
    if not fetch_ref(ROADMAP, refs / "roadmap"):
        raise Die(f"fetch {ROADMAP} failed")
    if not fetch_ref(REVIEW, refs / "review"):
        raise Die(f"fetch {REVIEW} failed")
    os.environ["TAUCETI_REQUIRE_TARGET_MARKER"] = "1"
    if bubble:
        return run_in_bubble(
            w,
            TAUCETI,
            fill_prompt(
                HERE / "prompts" / "roadmap.md",
                ONLY=only,
                SKIP=skip_str,
                AGENT=opts.agent_name,
                ROADMAP_DIR="/opt/roadmap/TauCetiRoadmap",
                REVIEW_DIR="/opt/review",
            ),
            opts,
            mounts=[f"{refs / 'roadmap'}:/opt/roadmap:ro", f"{refs / 'review'}:/opt/review:ro"],
        )  # M9
    if not prepare_checkout(w.cfg):
        raise Die("checkout failed")
    prompt = fill_prompt(
        HERE / "prompts" / "roadmap.md",
        ONLY=only,
        SKIP=skip_str,
        AGENT=opts.agent_name,
        ROADMAP_DIR=str(refs / "roadmap" / "TauCetiRoadmap"),
        REVIEW_DIR=str(refs / "review"),
    )
    return run_agent_host(w.cfg.checkout, prompt, opts.work_model, w.cfg.logdir)
