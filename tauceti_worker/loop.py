"""tauceti_worker.loop — the driver loop: pace against quota, run one round as a child under a hard
timeout, then settle (short pause if productive, escalating back-off otherwise)."""

from __future__ import annotations

import signal
import subprocess
import time

from .agents import resolve_authoring_profile
from .config import Config, NoProgress, log
from .constants import BACKOFF_BASE, BACKOFF_MAX, EX_NOPROGRESS, GH_MIN_BUDGET, INTERROUND, OPENROUTER_MODELS, POLL
from .github import github_budget
from .quota import Provider, Quota, _unavail_reason, quota_line
from .round import run_round_subprocess
from .runtime_status import report_runtime


class _LoopTerminated(KeyboardInterrupt):
    """SIGTERM translated to the same teardown path as Ctrl-C, with the right exit code."""


def _pace_wait_reason(window) -> str:
    """One soft pacing condition, formatted like quota.py without changing its control verdict."""
    relation = "=" if window.status == "at-budget" else ">"
    label = "at budget" if window.status == "at-budget" else "ahead of pace"
    comparison = (
        ""
        if window.used is None or window.budget is None
        else f" (used {round(window.used)}% {relation} {round(window.budget)}% budget)"
    )
    left = "" if window.used is None else f", {max(0, round(100 - window.used))}% left"
    return f"{window.name} {label}{comparison}{left}"


def _wait_quota_line(snap: dict) -> str:
    """Render the immediate pacing bottleneck when it defers an otherwise-initializable idle window.

    The provider remains HARD-blocked for launch-control purposes until the window is initialized; this
    is display-only. But when the only hard state is a plain post-reset idle window and a sibling window
    is pacing-blocked, the sibling is what must clear first. Show that condition first instead of hiding
    it behind the latent idle state.
    """
    line = quota_line(snap)
    prov = snap.get("claude")
    if prov is None or prov.error or prov.available:
        return line

    windows = prov.windows or []
    idle = [w for w in windows if w.status == "idle"]
    paced = [w for w in windows if w.status in ("at-budget", "over-pace")]
    other_hard = [w for w in windows if w.status not in ("under-pace", "at-budget", "over-pace", "idle")]
    if not idle or not paced or other_hard or any(w.detail != "window reset; awaiting initialization" for w in idle):
        return line

    why = "; ".join(
        [
            *(_pace_wait_reason(w) for w in paced),
            *(f"{w.name} window reset — initialization deferred until pacing permits" for w in idle),
        ]
    )
    old = quota_line({"claude": prov})
    new = f"claude [yellow]~[/] ({why})"
    return line.replace(old, new, 1)


def cmd_loop(args, cfg: Config, *, only: list[str], agent: str) -> int:
    """The driver: pace against quota (codex preferred), run ONE round as a child under a hard timeout,
    then settle (short pause if productive, escalating back-off otherwise). Ctrl-C stops the current
    round and exits. Keeps the escalating back-off that stopped ~700 no-op rounds hammering a
    rate-limited GitHub."""
    openrouter = agent in OPENROUTER_MODELS
    ignore_quota = getattr(args, "ignore_quota", False)
    bubble = getattr(args, "bubble", False)
    quota_cmd = getattr(args, "quota_cmd", None)
    log(f"loop start: worker={cfg.wid} only={','.join(only) or '(all)'} agent={agent}{' [bubble]' if bubble else ''}")
    report_runtime("idle", detail="loop started", phase=None, target=None, next_action_at=None)
    streak = 0
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signum, _frame) -> None:
        # run_round_subprocess catches KeyboardInterrupt and tears down the round's process
        # group before re-raising, so use that same proven cleanup path for Compose SIGTERM.
        raise _LoopTerminated

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            report_runtime("checking-quota", detail="checking provider availability", phase=None, target=None)
            # 1) Decide the model and whether to run this cycle. `pending_init` means Claude was picked
            # while a window of it is reset-but-unopened: the round is authorized to spend ONE small
            # request to open it, but only once it has found work (see work_units.dispatch).
            pending_init = False
            if openrouter:
                model = agent  # pay-per-token; no quota wait
            elif ignore_quota and not quota_cmd:
                # --ignore-quota overrides PACING (the soft over-pace throttle), not AVAILABILITY. We
                # still read the usage endpoint and wait out a HARD block: a window at 100% (exhausted),
                # usage we cannot read (fail-closed), or the endpoint itself refusing to answer (its own
                # 429 / a network failure). Only a soft over-pace block — real quota left, merely ahead of
                # the burn line — runs through here. Without this a pinned `--agent claude` worker re-fires
                # every green PR into a rate-limited subscription, burning a clone + engine launch each
                # round to post an all-error scoreboard.
                if agent == "auto":
                    raise SystemExit("--ignore-quota --loop needs an explicit --agent (codex/claude)")
                _chosen, snap = choose_model(cfg, agent, quota_cmd, refresh=True)
                prov = snap.get(agent)
                verdict = _ignore_quota_verdict(_chosen, prov)
                # An unopened window is the one hard block --ignore-quota may still clear, because the
                # clearing is a bounded, pace-respecting request rather than an override of a real limit.
                if verdict == "wait" and agent == "claude" and claude_pending_init(snap):
                    verdict, pending_init = "run", True
                if verdict == "wait":
                    why = prov.error if (prov and prov.error) else (_unavail_reason(prov)[1] if prov else "unavailable")
                    # Honor the endpoint's Retry-After, else wait for the blocking window's reset
                    # (capped), else poll. Never sooner than POLL, so we don't re-trip a 429.
                    nap = max(POLL, int(prov.retry_after) if (prov and prov.retry_after) else 0)
                    if prov and not prov.retry_after and prov.next_eligible:
                        nap = max(nap, min(int(prov.next_eligible - time.time()) + 5, 3600))
                    log(
                        f"quota: {agent} hard-blocked ({why}) — --ignore-quota still waits out a hard block; sleeping {nap}s"
                    )
                    report_runtime("waiting-quota", detail=why, next_action_at=time.time() + nap)
                    time.sleep(nap)
                    continue
                if verdict == "over-pace":
                    log(f"quota: {agent} over-pace; --ignore-quota set — running anyway")
                model = agent
            else:
                model, snap = choose_model(cfg, agent, quota_cmd, refresh=True)
                if model is None and claude_pending_init(snap):
                    model, pending_init = "claude", True
                if model is None:
                    # Honor a provider's Retry-After (e.g. a 429 asking for 580s) over the fixed poll, so
                    # we don't re-trip a rate limit by polling sooner than the server asked.
                    nap = max(POLL, max((p.retry_after or 0 for p in snap.values()), default=0))
                    if not any(p.retry_after for p in snap.values()):
                        eligible = [p.next_eligible for p in snap.values() if p.next_eligible]
                        if eligible:
                            # A forced refresh is valuable before a launch, not every five minutes
                            # throughout a known multi-hour wait. Recheck at least hourly so sibling
                            # workers or operator activity are still observed reasonably promptly.
                            nap = max(nap, min(int(min(eligible) - time.time()) + 5, 3600))
                    log(f"quota: {_wait_quota_line(snap)} — sleeping {nap}s")
                    report_runtime("waiting-quota", detail=_wait_quota_line(snap), next_action_at=time.time() + nap)
                    time.sleep(nap)
                    continue

            # 1b) GitHub budget preflight. A round does many gh calls AND launches the review engine,
            # whose own diff fetch 403s when GitHub is rate-limited — throwing away the agent's work.
            # The loop has no hard timeout, so this is where we wait out an hourly primary reset rather
            # than launching expensive work that a mid-round 403 would discard. We watch BOTH buckets a
            # round spends (REST core and the progress-guard graphql); either being low blocks launch
            # until the later of their resets. The rate_limit probe is itself exempt, so this is free
            # when we are flush.
            gb = github_budget()
            low = {k: v for k, v in (gb or {}).items() if v[0] < GH_MIN_BUDGET}
            if low:
                reset = max(v[1] for v in low.values())
                nap = max(POLL, min(reset - int(time.time()) + 5, 3600))
                detail = ", ".join(f"{k}={gb[k][0]}" for k in low)
                log(
                    f"github: REST budget low ({detail} remaining < {GH_MIN_BUDGET}) — "
                    f"waiting {nap}s for the reset before launching a round"
                )
                report_runtime("waiting-github", detail=detail, next_action_at=time.time() + nap)
                time.sleep(nap)
                continue

            # 2) Run ONE round as a child in its own process group, under the hard timeout.
            tail = ["--worker-id", cfg.wid]
            if only:
                tail += ["--only", ",".join(only)]
            if model:
                tail += ["--agent", model, "--ignore-quota"]  # loop already paced; child must not re-pace
            if pending_init:
                # Authorization travels with the round, not with the pacer: the child asks for it only
                # after it has surveyed and picked a work unit, so a round that finds nothing to do never
                # spends the request. The GitHub preflight above has already passed at this point.
                tail.append("--claude-bootstrap")
            if model:
                profile = resolve_authoring_profile(
                    model,
                    cli_model=getattr(args, "author_model", None),
                    cli_effort=getattr(args, "author_effort", None),
                )
                # Pin the exact parent-resolved profile into the isolated child. The
                # child must not re-read a different HOME or upstream CLI default. Preserve fallback
                # provenance separately: --author-model alone looks like an operator pin to the child.
                tail += ["--author-model", profile.model]
                if profile.effort:
                    tail += ["--author-effort", profile.effort]
                if profile.fallback_model:
                    tail += ["--resolved-author-fallback-model", profile.fallback_model]
            if bubble:
                tail.append("--bubble")
            source = getattr(args, "source", None)
            if source is not None:
                tail += ["--source", source]
            report_runtime("surveying", detail="selecting the next work unit", next_action_at=None)
            rc = run_round_subprocess(tail)

            # 3) Settle: productive → short pause; no-progress/timeout/error → escalating back-off.
            if rc == 0:
                streak = 0
                report_runtime(
                    "idle", detail="round completed", phase=None, target=None, next_action_at=time.time() + INTERROUND
                )
                time.sleep(INTERROUND)
            else:
                streak += 1
                nap = min(BACKOFF_BASE * (1 << min(streak, 5)), BACKOFF_MAX)
                tag = "timed out" if rc in (124, 137) else ("no progress" if rc == EX_NOPROGRESS else f"rc={rc}")
                log(f"round {tag}; no-progress streak={streak} — backing off {nap}s")
                report_runtime("backoff", detail=tag, phase=None, target=None, next_action_at=time.time() + nap)
                time.sleep(nap)
    except _LoopTerminated:
        log("loop terminated — stopping")
        return 143
    except KeyboardInterrupt:
        log("loop interrupted — stopping")
        return 130
    finally:
        report_runtime("stopping", detail="loop stopping", phase=None, target=None, next_action_at=None)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _ignore_quota_verdict(chosen: str | None, prov: Provider | None) -> str:
    """What a --ignore-quota loop does with the pacer snapshot for its pinned agent.

    --ignore-quota overrides PACING, not AVAILABILITY:
      "run"       — the provider is available (under pace), launch as usual.
      "over-pace" — a SOFT block: real quota remains, we are only ahead of the burn line. This is the
                    block --ignore-quota exists to override, so launch anyway.
      "wait"      — a HARD block: a window at 100% (exhausted), usage we cannot read (fail-closed), or
                    the usage endpoint refusing to answer (its own 429 / a network error leaves `prov`
                    with no windows). Firing here only hits a dead provider, so back off even under
                    --ignore-quota.
    """
    if chosen is not None:
        return "run"
    soft = prov is not None and _unavail_reason(prov)[0]
    return "over-pace" if soft else "wait"


def choose_model(cfg: Config, agent: str, quota_cmd: str | None, *, refresh: bool = False) -> tuple[str | None, dict]:
    """Decide which model to run now. With --quota-cmd / TAUCETI_QUOTA_CMD set, consult that external
    command instead of the built-in pacer (the escape hatch for e.g. a multi-account scheme): run
    `<quota_cmd> <agent>`; its first stdout token is the model to run (codex/claude/deepseek/minimax)
    or empty = none available. Otherwise use the self-contained pacer.

    This is a pure READ: choosing a model never spends quota. In particular an `auto` selection that
    inspects Claude and then picks codex makes no Claude request."""
    if quota_cmd:
        import shlex

        r = subprocess.run(shlex.split(quota_cmd) + [agent], capture_output=True, text=True)
        out = (r.stdout or "").split()
        model = out[0] if (r.returncode == 0 and out) else None
        return (model or None), {"quota-cmd": Provider("quota-cmd", bool(model), model)}
    return Quota(cfg).choose(None if agent == "auto" else agent, refresh=refresh)


def claude_pending_init(snap: dict) -> bool:
    """True when Claude is unavailable ONLY because a window has reset and nothing has opened the next
    cycle yet, and initializing it is within the operator's pace curve (Quota decides both, purely).

    Such a provider is not "out of quota" — it is unopened, and only a Claude request can open it. It
    may therefore be selected PROVISIONALLY: nothing is spent by selecting it, and the round makes the
    one bootstrap request at its launch stage, after it has found actual work to do."""
    prov = snap.get("claude")
    return bool(prov and prov.bootstrap_eligible)


def resolve_work_model(
    cfg: Config, agent: str, *, dry: bool, ignore_quota: bool, quota_cmd: str | None = None
) -> tuple[str, bool]:
    """Turn the --agent dial into (concrete model, needs-launch-stage-bootstrap). 'auto' consults the
    pacer (or --quota-cmd); codex preferred, opus fallback. OpenRouter agents are pay-per-token (no
    pacing). Dry-run symbolic. The bootstrap flag never launches anything by itself — it says the round
    must ask for launch authorization once it has a work unit in hand."""
    if dry:
        return agent, False
    if agent in OPENROUTER_MODELS:
        return agent, False
    if ignore_quota and not quota_cmd:
        if agent == "auto":
            raise SystemExit(
                "--ignore-quota needs an explicit --agent (codex/claude); 'auto' can't choose without the pacer"
            )
        return agent, False
    chosen, snap = choose_model(cfg, agent, quota_cmd)
    if chosen is None and claude_pending_init(snap):
        return "claude", True
    if chosen is None:
        raise NoProgress(f"no model under pace right now (agent={agent}) — nothing to run this round")
    return chosen, False
