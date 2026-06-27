"""tauceti_worker.constants — repo names, per-PR budgets, loop timing, rate-limit regexes, and the
agent/task tables."""

from __future__ import annotations

import os
import re

TAUCETI = "FormalFrontier/TauCeti"

TAUCETI_OWNER = TAUCETI.split("/", 1)[0]  # base-repo owner: a bot PR is first-party iff its head lives here

ROADMAP = "FormalFrontier/TauCetiRoadmap"

REVIEW = "FormalFrontier/TauCetiReview"


# Per-PR budgets (a PR can never churn forever).
MAX_FIX_ATTEMPTS = 3  # per-head: stop re-running the fixer on a commit it can't change (a stuck

# head never advances a review round, so CI's round cap can't catch it).
# The review-ROUND budget lives in CI now (TauCeti housekeeping closes a PR reviewed to its cap while
# still blocking). The worker no longer caps its own review rounds — it keeps reviewing on every new
# head until the PR merges or CI closes it — so every PR reaches a terminal state.
MAX_REVIEW_ERRORS = 3  # per PR: after this many review rounds that ERROR without posting a verdict

# (the engine can't produce a review at all), stop retrying and ESCALATE —
# a loud per-round warning + a tracking issue — since a PR that can never be
# reviewed neither merges nor reaches CI's round cap, so a human must step in.
MAX_REVIEW_CONTESTS = 10  # per-PR lifetime cap on author-contest re-reviews (no-bar: anyone may

# reply, so this bounds spend; separate from the review-round budget)
MAX_REVIEW_CONTESTS_PER_RUBRIC = 3  # per-rubric cap so one noisy thread can't starve the PR's budget

# The review ENGINE enforces a per-PR daily round cap (its --max-rounds-per-day, default 12); once hit it
# refuses to review but still exits 0 after re-posting the scoreboard. The worker mirrors that number so it
# can SKIP a capped PR during the survey — before launching the engine (and its expensive clones) — instead
# of re-selecting it every round and tight-looping. MUST stay in sync with the engine default (review.py).
REVIEW_DAILY_CAP = int(os.environ.get("TAUCETI_REVIEW_DAILY_CAP", "12"))

CONTEST_CLAIM_TTL = 3600  # seconds a 👀 on the contested reply claims an in-flight contest re-review.

# The claim lives on GitHub (a reaction on the reply comment), so it dedups
# ACROSS the fleet — unlike a per-worker counter, which can't coordinate
# between isolated stores. Removed once the round publishes; this TTL is only
# the backstop that frees a claim left by a worker that crashed mid-review.
CONTEST_CLAIM_EMOJI = "eyes"

MAX_CI_ATTEMPTS = 3  # per-head: stop trying to green a red-CI head

MAX_CI_PR_ATTEMPTS = 5  # per-PR lifetime backstop for red-CI fixing

MAX_REBASE_ATTEMPTS = 3  # per-PR: stop trying to rebase a conflicting PR

MAX_BUMP_ATTEMPTS = 3  # per-head: stop trying to green a red bump-mathlib head

MAX_BUMP_PR_ATTEMPTS = 5  # per-PR lifetime backstop for bump fixing across heads

BUMP_HEAD_PREFIX = "bump-mathlib/"  # branch prefix the review bot opens its mathlib-bump PRs on

MAX_OPEN_PRS = 8  # backpressure: don't author new roadmap PRs while this many of ours are open


# Loop timing. Env-overridable for tuning and tests.
POLL = int(os.environ.get("TAUCETI_POLL", "300"))  # seconds between quota checks while waiting

ROUND_TIMEOUT = int(os.environ.get("TAUCETI_ROUND_TIMEOUT", "5400"))  # 90 min hard cap per round

INTERROUND = int(os.environ.get("TAUCETI_INTERROUND", "20"))  # min gap after a PRODUCTIVE round

EX_NOPROGRESS = 75  # round did NO productive work (distinct from error=1 / success=0)

BACKOFF_BASE = int(os.environ.get("TAUCETI_BACKOFF_BASE", "30"))  # first no-progress sleep (doubles each round)

BACKOFF_MAX = int(os.environ.get("TAUCETI_BACKOFF_MAX", "900"))  # cap on the escalating sleep (15 min)

# The escalating back-off exists because a no-op round must NOT re-cycle every INTERROUND seconds and
# re-hammer the API — the failure that ran ~700 no-op rounds against a rate-limited GitHub.

# GitHub REST rate-limit handling. We pace LLM quota carefully; GitHub's REST budget needs the same
# care, or a 403 mid-round throws away the agent's (expensive) work. Two layers:
#  - gh_run() waits IN PLACE for a rate limit to clear and retries (so a transient limit costs a pause,
#    not a lost round), bounded by GH_INROUND_WAIT so it can't blow ROUND_TIMEOUT.
#  - cmd_loop preflights core budget BEFORE launching a round (no hard cap there) — the right place to
#    wait out an hourly primary reset, and what keeps us from launching the review engine (whose own
#    diff fetch would 403) without enough budget to finish.
GH_MIN_BUDGET = int(os.environ.get("TAUCETI_GH_MIN_BUDGET", "200"))  # core requests a round needs to finish

GH_INROUND_WAIT = int(os.environ.get("TAUCETI_GH_INROUND_WAIT", "900"))  # cap on gh_run's in-place wait (15 min)

GH_SECONDARY_BASE = 60  # first secondary-limit sleep when no Retry-After is given (then exponential)

_GH_PRIMARY_RE = re.compile(r"(?:API )?rate limit exceeded|rate limit.*exceeded", re.I)

_GH_SECONDARY_RE = re.compile(r"secondary rate limit|abuse detection", re.I)


# Claims / scoreboard cache.
CLAIM_TTL_S = int(os.environ.get("CLAIM_TTL", "1500"))  # 25 min lease; expires if a worker stops heartbeating

CLAIM_HEARTBEAT_S = int(os.environ.get("CLAIM_HEARTBEAT", "300"))  # renew every 5 min while the agent runs

SBCACHE_TTL = int(os.environ.get("TAUCETI_META_TTL", "120"))  # seconds a cached scoreboard meta stays fresh

COMMENTS_MEMO_S = 5  # in-memory window over which one survey pass coalesces its issue-comment fetches

# (scoreboard meta + in-flight marker share one read); << the round/dashboard cadence

# In-flight review de-contention. The review engine (TauCetiReview) posts a PR comment marking a head
# as under review and embeds an `expires_at` so a crashed reviewer self-clears. De-contention is on the
# head ALONE (a commit is reviewed once, regardless of model), and the engine's coordinate() remains the
# authoritative claim. The worker reads the SAME marker during the survey so it can skip a head a peer is
# already reviewing BEFORE paying the engine's build+launch cost — and, crucially, without busy-looping
# on the one PR a peer holds. The marker format is owned by the engine; we parse only the head and the
# expiry, so the engine's TTL value stays its own concern.
REVIEW_INPROGRESS_RE = re.compile(r"<!--tauceti-review-in-progress (.*?)-->", re.S)


# Agents.
OPENROUTER_MODELS = {
    "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek/deepseek-v4-pro"),
    "minimax": os.environ.get("MINIMAX_MODEL", "minimax/minimax-m3"),
}

AGENT_NAMES = {"codex": "Codex", "claude": "Claude Code", "deepseek": "DeepSeek", "minimax": "MiniMax"}

PI_RUN = os.environ.get("PI_RUN", os.path.expanduser("~/.claude/skills/pi/scripts/run.sh"))

# $TAUCETI_CLAUDE_CMD overrides the `claude` executable for host rounds (a sandbox wrapper, a
# differently-named build, ...); it's split as a shell word list and the standard
# -p/--model/--permission flags are still appended. Matches PI_RUN / $TAUCETI_BUBBLE /
# $TAUCETI_CODEX_MODEL. (Bubble rounds run claude inside the container, so this is --host only.)
CLAUDE_CMD = os.environ.get("TAUCETI_CLAUDE_CMD", "claude")


# Task taxonomy. Every task drives a model; merge/abandon/dedup housekeeping lives in the repo's CI now.
ALLOWED_TASKS = ["rebase", "review", "fix-ci", "fix", "bump", "roadmap"]

WORK_TASKS = list(ALLOWED_TASKS)

# The "#" shown in the survey table IS the key you press in the TUI to run one round of that kind.
# Both derive from ALLOWED_TASKS so the table number, the row order, and the keybinding can never drift.
KIND_KEYS = {str(i): name for i, name in enumerate(ALLOWED_TASKS, 1)}  # "1" -> "rebase", ...

KIND_BY_NAME = {name: num for num, name in KIND_KEYS.items()}  # "rebase" -> "1", ...

# Every mode runs a MODEL on third-party content, so each defaults to bubble (opt out with --host).
SANDBOX_DEFAULT = {t: True for t in WORK_TASKS}


AGENTS = ["auto", "codex", "claude", "deepseek", "minimax"]
