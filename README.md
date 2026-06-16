# Tau Ceti Worker

An autonomous loop that keeps the [TauCeti](https://github.com/FormalFrontier/TauCeti)
Lean library moving forward on your Claude Max / Codex subscriptions. It runs
unattended: each round it picks the single most useful thing to do right now —
land a green PR, review an open one, fix a PR a review flagged, bump Mathlib, or
author a new PR against the roadmap — does exactly that one thing, and sleeps
until quota allows the next round.

You can let it **do whatever is most helpful** (the default), or pin it to a
**specific mode** — one kind of work, one model, sandboxed or not. Both are one
command; see [Modes](#modes).

> The worker is wired to one deployment: it acts as `kim-em` on
> `FormalFrontier/TauCeti`. The repo/owner are not configurable knobs — this is
> an operator's tool for that project, not a general framework.

## What one round does

Every round performs **one** unit of work, chosen in strict priority order. The
first applicable step wins; the rest wait for a later round.

| # | Step | What it does |
|---|------|--------------|
| 0 | **Housekeeping** | *(always, quota-free)* Merge every PR already green-lit by review; close PRs that spent their whole review budget without going green; close duplicate roadmap PRs. CI-side merge is intentionally off, so the worker is the only thing that lands or retires PRs. |
| 1 | **Rebase** | Resolve a conflicting PR of ours (usually the root `TauCeti.lean` import line, after a sibling merged first). |
| 2 | **Review** | Review an open PR whose current head is green but not yet reviewed, via the `tauceti-review` CLI. |
| 3 | **Fix** | Address the findings on one of our PRs whose latest review requests changes (🟡) or blocks (⛔) — fix the code, or contest a wrong finding on its thread. |
| 4 | **Fix CI** | Green a PR of ours whose `build` check has failed at head (it can't be reviewed until it builds). |
| 5 | **Bump** | If Mathlib `master` has moved past our pin and no bump PR is open, bump `lean-toolchain` + `lake-manifest.json` to master and fix whatever breaks in `TauCeti/`. |
| 6 | **Roadmap** | Otherwise, start a new PR advancing a [roadmap](https://github.com/FormalFrontier/TauCetiRoadmap) target. |

A GitHub API failure **aborts** the round rather than being read as "nothing to
do" — a transient outage never silently falls through to authoring.

## Quickstart

```bash
./loop.sh          # do whatever is most helpful, on your subscriptions
```

That's the whole thing. It runs forever, one round at a time, until you press
Ctrl-C (which stops the current round and exits). Per-round output lands in
`logs/<worker-id>/<task>-<timestamp>.log`.

To try a single round without committing to the loop:

```bash
./round.sh         # perform exactly one unit of work, then exit
```

Before the first run, make sure the [prerequisites](#prerequisites) are on
`PATH` and you are logged in. `loop.sh` runs a preflight at startup and exits
loudly if anything it needs is missing, so a misconfigured host fails fast
instead of sleeping forever.

## Modes

There are three independent dials. Leave them all at their defaults and the
worker does whatever is most helpful, on whatever model has quota, on the host.
Turn any of them and you get a focused worker. They combine freely.

### 1. What work — `--only` (default: the whole cascade)

By default a round walks the full priority cascade above and does the first
applicable step. Pass `--only <task>[,<task>...]` to confine it to certain kinds
of work:

```bash
./loop.sh --only review            # a reviewer: only review open PRs
./loop.sh --only roadmap           # an author: only open new roadmap PRs
./loop.sh --only fix,fix-ci        # a maintainer: only tend to our own PRs
./loop.sh --only bump              # only keep us current with Mathlib master
./loop.sh --only merge             # a janitor: only land/abandon/dedup (no model)
```

Valid tasks are the step names: `rebase`, `review`, `fix`, `fix-ci`, `bump`,
`roadmap`, and `merge`. The **housekeeping pre-passes always run** in every mode
— a focused worker still lands green PRs and clears stuck/duplicate ones — so the
queue stays healthy no matter what each worker is pinned to.

`--only merge` is the special case: it names *housekeeping only*, drives no
model, and so skips the quota wait entirely. It's a cheap "janitor" you can run
alongside model-spending workers to keep the queue draining. A focused worker
that finds nothing to do in its allowed steps reports no-progress and backs off,
rather than falling through to authoring.

### 2. Which model — `--codex` / `--claude` / … (default: auto)

By default the worker uses your subscriptions, preferring **Codex** to spare the
more precious **Opus** quota, and waits when neither is available. An explicit
flag pins **both** authoring/fixing and reviewing to one model:

| Flag | Model | Quota / billing |
| --- | --- | --- |
| _(none)_ | Codex preferred, Opus fallback (for both authoring and review) | subscription |
| `--codex` | Codex (`gpt-5.5`) only | subscription (waits on Codex quota) |
| `--claude` | Opus only | subscription (waits on Opus quota) |
| `--deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | **pay-per-token** (`OPENROUTER_API_KEY`) |
| `--minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | **pay-per-token** (`OPENROUTER_API_KEY`) |

The OpenRouter models (DeepSeek, MiniMax) are driven through the `pi` agentic
loop, which runs arbitrary models Claude Code / Codex can't drive natively. They
are **pay-per-token, so there is no auto-dispatch** — they run *only* when you
pass their flag (the flag is the budget gate). Override a model id with
`DEEPSEEK_MODEL` / `MINIMAX_MODEL`; point at a non-default `pi` runner with
`PI_RUN`. Adding another OpenRouter model is one entry in the `OPENROUTER_MODELS`
map in `round.sh` (and the matching one in `review.py`).

> **Why these models.** `deepseek/deepseek-v4-pro` and `minimax/minimax-m3` are
> each provider's strongest agentic, tool-using model on OpenRouter (tool use,
> reasoning, 1M-token context). DeepSeek-Prover-V2 and ByteDance Seed-Prover top
> the [Lean eval leaderboard](https://lean-lang.org/eval) but are **whole-proof
> search systems, not tool-using agents** — they prove a given statement, they
> don't review PRs or author library code — and neither is served on OpenRouter,
> so neither can drive `pi`. Hence the general flagship models.

### 3. Where it runs — `--bubble` (default: host)

By default authoring/fixing runs on the host: fast and simple, but the agent has
the host's full git/gh credentials and network. Pass `--bubble` to run each
authoring/fixing round inside a [`bubble`](https://github.com/kim-em/bubble)
container, repo-scoped to `FormalFrontier/TauCeti`, so a misbehaving or
prompt-injected agent is bounded by the container rather than the host. See
[Sandboxing](#sandboxing---bubble) for what that buys you.

### Combining

The dials are orthogonal. Some examples:

```bash
./loop.sh                                  # auto model, all work, on the host
./loop.sh --bubble --codex                 # sandboxed, Codex only, all work
./loop.sh --only review --claude           # an Opus-only reviewer
./loop.sh --only roadmap --bubble          # a sandboxed roadmap author
./loop.sh --only merge                     # a quota-free janitor
./round.sh --only review                   # one review round, then exit
```

## Prerequisites

On `PATH`, logged in to the subscriptions you want used:

- **Always:** `gh` (as `kim-em`), `git`, and `jq`. (`--only merge` needs nothing
  more — it only talks to the GitHub API.)
- **Reviewing:** `uv`/`uvx` (the `tauceti-review` engine runs via `uvx`).
- **Host authoring** (the default): an `elan`/`lake` toolchain on the host (the
  per-round build runs there).
- **`--bubble`** (sandboxed authoring): [`bubble`](https://github.com/kim-em/bubble)
  and a working Incus runtime — the Lean toolchain then lives in the container
  image, not on the host.
- **Subscription models:** `claude` (Claude Code, Opus) and/or `codex` (ChatGPT)
  on the host (bubble also bakes them into the image and seeds their
  credentials), plus the `claude-usage` skill scripts at
  `~/.claude/skills/claude-usage/`.
- **`--deepseek` / `--minimax`:** the [`pi`](https://github.com/badlogic/pi-mono)
  agent on `PATH` (the `pi` skill wrappers at `~/.claude/skills/pi/`) and
  `OPENROUTER_API_KEY` **exported** — it lives in `~/.zshrc`, which a
  non-interactive shell does not source, so export it before launching the loop.
  (`pi` is required on the host even with `--bubble` — bubble bakes it into the
  container image when present, exactly as it does `claude` / `codex`.)

## Quota policy (auto mode)

A round runs only while subscription quota is available, preferring **Codex** to
spare the more precious **Opus** quota; it sleeps and re-checks otherwise.
Sonnet alone does **not** count — the worker wants Opus or Codex. (It uses the
`claude-available-model` / `codex-available-model` scripts from the
`claude-usage` skill, and `swap-account best` to read and run under whichever
Claude account has the most quota.) Both authoring/fixing and review use a
single provider, Codex preferred and Opus as fallback — review never runs both
at once, because the engine splits rubrics across providers at random and one
provider's outage would then silently error half the rubrics.

A productive round is followed by a short pause; an unproductive one (nothing to
do, backpressure, or a transient GitHub failure) triggers an **escalating
back-off** so a stuck queue or a rate-limited GitHub is probed ever more slowly
instead of being re-hammered. Each round runs under a 90-minute hard timeout in
its own process group, so a wedged sub-task is torn down rather than parking the
loop.

## How a round decides (round.sh)

- "Needs review" / "needs fix" are read from the PR's canonical **scoreboard
  comment on GitHub** (the multi-agent source of truth), with the local
  `tauceti-review` store as a cache: a PR needs review when its current head
  isn't the last reviewed head; it needs a fix when the latest review at the
  current head carries a 🟡/⛔ rubric. Each step is bounded (`state/fix-*`,
  `state/ci-*`, `state/rebase-pr-*`, review-round and bump budgets) so no PR can
  churn indefinitely; a PR that exhausts its review budget without going green is
  closed by the housekeeping pre-pass (branch kept, so it can be revived).
- Authoring/fixing runs **on the host by default** (a reused checkout at
  `checkouts/<worker-id>/TauCeti`, cleaned to `origin/main` each round, `.lake`
  preserved for fast builds), or **inside a `bubble` container** with `--bubble`.
  Review rounds always run on the host — the `tauceti-review` CLI has its own
  clean room.
- The agent is driven by the prompt templates in `prompts/` (`bump.md`,
  `fix.md`, `fix-ci.md`, `rebase.md`, `roadmap.md`), with per-round
  substitutions. Agents run with full tool access (Claude with
  `--dangerously-skip-permissions` and `ANTHROPIC_API_KEY` unset so it bills the
  Max plan; Codex with `--sandbox danger-full-access`; DeepSeek/MiniMax through
  `pi` against OpenRouter, billed per-token).
- Authoring is confined to one roadmap area via `ROADMAP_FOCUS` (default
  `ReductiveGroups` in `round.sh`; set it empty to range over all areas), and
  held back by backpressure once the worker already has `MAX_OPEN_PRS` open.

## Sandboxing (`--bubble`)

Pass `--bubble` to run each authoring/fixing round inside a
[`bubble`](https://github.com/kim-em/bubble) container:

- **Filesystem** — the agent sees only the in-container checkout plus the
  read-only reference mounts the round stages for it (the prompt at `/opt/round`,
  and for roadmap rounds the `TauCetiRoadmap` / `TauCetiReview` clones at
  `/opt/roadmap` / `/opt/review`). It cannot read host files outside the
  workspace, including `~/.claude/CLAUDE.md` or other repositories.
- **GitHub** — the container never sees the host `kim-em` token. All `git`/`gh`
  traffic goes through bubble's auth proxy, repo-scoped to
  `FormalFrontier/TauCeti` — a push or API call outside that repo is rejected by
  the proxy, not merely flagged by CI after the fact. The public reference repos
  are staged on the host and mounted read-only; Mathlib is the checkout's own
  vendored Lake dependency.
- **Credentials** — only the one credential the work model needs is seeded:
  `~/.claude/.credentials.json` *or* `~/.codex/auth.json` for the subscriptions,
  or — for `--deepseek` / `--minimax` — the `OPENROUTER_API_KEY` mounted
  read-only at `/opt/round/openrouter.key`. The other models' credentials and all
  host config (CLAUDE.md, skills, Codex config) stay out.
- **Isolation from operator config** — the worker drives bubble with a private
  `BUBBLE_HOME` (`~/.cache/tauceti-worker/bubble`, override via
  `$TAUCETI_BUBBLE_HOME`) and `--local`, so a round can't inherit ambient
  `[[mounts]]` or a remote/cloud default from your `~/.bubble/config.toml`. The
  shared Mathlib cache is `overlay` (read-only base + per-round writable overlay)
  so one round can't poison a later round's build. First use builds the worker's
  git mirrors and cache there — slow once, fast afterwards.
- **Teardown** — the container is `--ephemeral` (popped when the round's command
  exits, propagating its exit code), popped again explicitly in case that failed,
  and any leftover from a SIGKILLed round is cleared at the start of the next
  round. `round.sh` also takes a `flock` so two rounds can't run at once.

> **`--bubble` with `--deepseek` / `--minimax`** needs the `pi` tool in the
> bubble image (which also allowlists `openrouter.ai` egress); that lands in
> [kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299). Until then,
> sandboxed OpenRouter rounds fail at `pi: command not found`; run them on the
> host in the meantime. `--bubble` with `--codex` / `--claude` works today.

Review rounds are unchanged: the `tauceti-review` CLI already runs each reviewer
model in a clean room (only its own credential, no host config) and only
reads/greps — it never builds or pushes. Reviewing with DeepSeek/MiniMax also
needs the engine to know the provider; that landed in
[TauCetiReview#42](https://github.com/FormalFrontier/TauCetiReview/pull/42).

## Running many workers on one host

Each loop namespaces its state, checkout, review store, and logs under a worker
id, so N copies on one host don't collide. For **uncoordinated multi-worker**
use, every loop must have a globally-unique id and isolated auth state:

```bash
./loop.sh --worker-id alice --isolate-home --only review
./loop.sh --worker-id bob   --isolate-home --only roadmap
```

- `--worker-id <id>` (or `TAUCETI_WORKER_ID`) pins a stable name; otherwise a
  fresh `hostname-<uuid>` id is minted per start (never persisted to a shared
  file — two concurrent loops must never share an id).
- `--isolate-home` gives each worker its own mutable Claude/Codex auth state
  (symlinking the read-only skill/tool surface from your real home) so two
  workers can't race and corrupt `~/.claude/.current-account`. **Exercise this on
  a live host before relying on it** — it touches credential plumbing the script
  can't unit-test.

Coordination between workers is through GitHub itself: the per-PR scoreboard
comment is the shared review state, a create-only push arbiter (`git-safe-push`
/ `gh-safe-pr-create`) serializes writes, and `claim.sh` provides optional
cooperative de-contention. A worker only ever merges/abandons/dedups under
fail-safe guards (human-activity and `keep`-label checks), so adding workers
grows throughput without two of them clobbering each other.

## Other notes

- `loop.sh` runs a `preflight` at startup (checks `gh`/`git`/`jq`, plus the
  pieces each mode needs — `uvx`, `lake` or `bubble`, the model CLI / `pi` +
  `OPENROUTER_API_KEY`, the quota scripts, and `gh auth`) and exits loudly if
  anything is missing.
- `checkouts/`, `state/`, and `logs/` are runtime-only and git-ignored.
