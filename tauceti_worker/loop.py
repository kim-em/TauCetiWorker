"""tauceti_worker.loop — split from the monolithic worker (behaviour-preserving)."""

from __future__ import annotations

import subprocess
import time

from .config import Config, NoProgress, log
from .constants import BACKOFF_BASE, BACKOFF_MAX, EX_NOPROGRESS, GH_MIN_BUDGET, INTERROUND, OPENROUTER_MODELS, POLL
from .github import github_budget
from .quota import Provider, Quota, quota_line
from .round import run_round_subprocess


def cmd_loop(args, cfg: Config, *, only: list[str], agent: str) -> int:
    """The driver: pace against quota (codex preferred), run ONE round as a child under a hard timeout,
    then settle (short pause if productive, escalating back-off otherwise). Ctrl-C stops the current
    round and exits. Mirrors loop.sh — including the back-off that stopped ~700 no-op rounds hammering
    a rate-limited GitHub."""
    openrouter = agent in OPENROUTER_MODELS
    ignore_quota = getattr(args, "ignore_quota", False)
    host = getattr(args, "host", False)
    quota_cmd = getattr(args, "quota_cmd", None)
    log(f"loop start: worker={cfg.wid} only={','.join(only) or '(all)'} agent={agent}{' [host]' if host else ''}")
    streak = 0
    try:
        while True:
            # 1) Decide the model and whether to run this cycle.
            if openrouter:
                model = agent  # pay-per-token; no quota wait
            elif ignore_quota and not quota_cmd:
                if agent == "auto":
                    raise SystemExit("--ignore-quota --loop needs an explicit --agent (codex/claude)")
                model = agent
            else:
                model, snap = choose_model(cfg, agent, quota_cmd)
                if model is None:
                    # Honor a provider's Retry-After (e.g. a 429 asking for 580s) over the fixed poll, so
                    # we don't re-trip a rate limit by polling sooner than the server asked.
                    nap = max(POLL, max((p.retry_after or 0 for p in snap.values()), default=0))
                    log(f"quota: {quota_line(snap)} — sleeping {nap}s")
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
                time.sleep(nap)
                continue

            # 2) Run ONE round as a child in its own process group, under the hard timeout.
            tail = ["--worker-id", cfg.wid]
            if only:
                tail += ["--only", ",".join(only)]
            if model:
                tail += ["--agent", model, "--ignore-quota"]  # loop already paced; child must not re-pace
            if host:
                tail.append("--host")
            rc = run_round_subprocess(tail)

            # 3) Settle: productive → short pause; no-progress/timeout/error → escalating back-off.
            if rc == 0:
                streak = 0
                time.sleep(INTERROUND)
            else:
                streak += 1
                nap = min(BACKOFF_BASE * (1 << min(streak, 5)), BACKOFF_MAX)
                tag = "timed out" if rc in (124, 137) else ("no progress" if rc == EX_NOPROGRESS else f"rc={rc}")
                log(f"round {tag}; no-progress streak={streak} — backing off {nap}s")
                time.sleep(nap)
    except KeyboardInterrupt:
        log("loop interrupted — stopping")
        return 130


def choose_model(cfg: Config, agent: str, quota_cmd: str | None) -> tuple[str | None, dict]:
    """Decide which model to run now. With --quota-cmd / TAUCETI_QUOTA_CMD set, consult that external
    command instead of the built-in pacer (the escape hatch for e.g. a multi-account scheme): run
    `<quota_cmd> <agent>`; its first stdout token is the model to run (codex/claude/deepseek/minimax)
    or empty = none available. Otherwise use the self-contained pacer."""
    if quota_cmd:
        import shlex

        r = subprocess.run(shlex.split(quota_cmd) + [agent], capture_output=True, text=True)
        out = (r.stdout or "").split()
        model = out[0] if (r.returncode == 0 and out) else None
        return (model or None), {"quota-cmd": Provider("quota-cmd", bool(model), model)}
    return Quota(cfg).choose(None if agent == "auto" else agent)


def resolve_work_model(cfg: Config, agent: str, *, dry: bool, ignore_quota: bool, quota_cmd: str | None = None) -> str:
    """Turn the --agent dial into the concrete model to run. 'auto' consults the pacer (or --quota-cmd);
    codex preferred, opus fallback. OpenRouter agents are pay-per-token (no pacing). Dry-run symbolic."""
    if dry:
        return agent
    if agent in OPENROUTER_MODELS:
        return agent
    if ignore_quota and not quota_cmd:
        if agent == "auto":
            raise SystemExit(
                "--ignore-quota needs an explicit --agent (codex/claude); 'auto' can't choose without the pacer"
            )
        return agent
    chosen, _snap = choose_model(cfg, agent, quota_cmd)
    if chosen is None:
        raise NoProgress(f"no model under pace right now (agent={agent}) — nothing to run this round")
    return chosen
