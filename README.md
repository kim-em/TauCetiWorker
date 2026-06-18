# Tau Ceti Worker

`tauceti` keeps the [TauCeti](https://github.com/FormalFrontier/TauCeti) Lean
library moving, using a "bring your own agent" approach. Run it with no command
and you get a dashboard of the work the queue needs right now: PRs to review,
fixes a review asked for, a Mathlib bump that needs adapting, roadmap targets.
From there you launch whatever you want. Pin a worker to one kind of work with
`--only` (a reviewer, a fixer, an author), or hand the whole thing to `--loop`
and let it pick the most useful job each round until you stop it.

It runs as your authenticated `gh` account: you set up `gh auth`, the worker acts
as that account, and it treats that account's own PRs as the ones it tends. The
repo is hardwired to `FormalFrontier/TauCeti`. This is an operator's tool for that
project, not a general framework.

## Quickstart

Install it as a tool, no clone needed:

```bash
uv tool install git+https://github.com/kim-em/TauCetiWorker.git

tauceti                            # the dashboard: see the available work, launch it
tauceti status                     # the same survey, non-interactive (--json for scripts)
tauceti work --only review         # one round of a specific kind of work, then exit
tauceti work --loop --only review  # a focused worker: keep reviewing (or fix / roadmap / ...)
tauceti work --loop                # fully automatic: keep picking the most useful job
```

From a clone of this repo you can also just run `./tauceti` (it's a single `uv`
script), and every command above works the same. Either way, Ctrl-C stops the
current round and exits, and `tauceti doctor` checks your environment and tells
you what's missing.

### The dashboard

Bare `tauceti` opens an interactive dashboard. The table lists each kind of work
with a number, how many PRs are ready, and a sample; a cursor highlights one row.
It reacts to single keypresses (no Enter):

| Key | Action |
|-----|--------|
| `↑` / `↓` | move the cursor between kinds |
| `→` / `←` | expand / collapse the selected kind — list its PRs with titles (or, on `roadmap`, the focus areas) |
| `Enter` | run one round of the selected kind |
| `1`–`6` | run one round of that numbered kind directly |
| `l` / `L` | loop the auto cascade / loop just the selected kind |
| `f` | pick the roadmap focus from the available areas |
| `m` / `s` | cycle the agent / toggle the sandbox (bubble ↔ host) |
| `r` / `c` / `q` | refresh / copy the launch command / quit |

Over a pipe or with no TTY it prints a one-shot snapshot instead (use `tauceti
status` in scripts).

## What a round does

A round does exactly one unit of work: the first of these that applies.

| Step | What it does |
|------|--------------|
| **Rebase** | Resolve one of our conflicting PRs — a genuine content conflict under `TauCeti/` after a sibling merged first (the root `TauCeti.lean` is auto-synced on `main`, so it no longer collides). |
| **Review** | Review an open PR whose head is green but not yet reviewed, with the `tauceti-review` engine. |
| **Fix CI** | Green one of our PRs whose `build` check is red. It can't be reviewed until it builds, so this comes before Fix. |
| **Fix** | Address the review findings on one of our PRs: fix the code, or contest a wrong finding on its thread. |
| **Bump** | Adapt a red `hopscotch/lkg-bump` PR (the [hopscotch bot](https://github.com/leanprover-community/hopscotch) opens those to move the Mathlib dependency forward) so `TauCeti/` builds against the new Mathlib. The worker never opens a bump itself. |
| **Roadmap** | Otherwise, open a new PR advancing a [roadmap](https://github.com/FormalFrontier/TauCetiRoadmap) target. |

Merging green PRs, closing stuck ones, and de-duplicating are the repo's CI, not
the worker. A GitHub API failure aborts the round rather than reading as "nothing
to do", so a transient outage never falls through to authoring.

## Modes

There are three dials: which work (`--only`), which agent (`--agent`), and where
it runs (`--host`). They're independent, so combine them however you like.

### What work: `--only`

With no `--only`, a round walks the whole cascade and does the first job that
applies. `--only <task>[,<task>...]` pins it to particular kinds:

```bash
tauceti work --loop --only review     # only review open PRs
tauceti work --loop --only roadmap    # only open new roadmap PRs
tauceti work --loop --only fix,fix-ci # only tend to our own PRs
tauceti work --loop --only bump       # only adapt broken hopscotch bump PRs
```

The tasks are `rebase`, `review`, `fix-ci`, `fix`, `bump`, `roadmap`.

Roadmap rounds steer toward one focus area (a subdirectory of the
[roadmap](https://github.com/FormalFrontier/TauCetiRoadmap)). Set it with
`--roadmap-focus <area>` (or `TAUCETI_ROADMAP_FOCUS`, or the dashboard's `f`
key); an empty value means "all areas".

### Which agent: `--agent`

`--agent` is independent of `--only`, so any kind of work can run on any agent:

| `--agent` | Model | Billing |
| --- | --- | --- |
| `auto` (default) | Codex preferred, Opus fallback | subscription, paced |
| `codex` | Codex only | subscription, paced |
| `claude` | Opus only | subscription, paced |
| `deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | pay-per-token (`OPENROUTER_API_KEY`) |
| `minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | pay-per-token (`OPENROUTER_API_KEY`) |

Set a default with `TAUCETI_AGENT`. The OpenRouter agents are pay-per-token, so
they never run on their own; you have to ask for them by name. Override their
model ids with `DEEPSEEK_MODEL` / `MINIMAX_MODEL`, and point at a non-default `pi`
runner with `PI_RUN`.

### Where it runs: bubble, or `--host`

Every round runs its agent inside a [`bubble`](https://github.com/kim-em/bubble)
sandbox by default. That's a repo-scoped container: your `gh` token never enters
it (git and gh go through bubble's auth proxy), only the one credential the agent
needs is seeded, and none of your host config crosses the boundary. That matters
most for review, where the agent reads untrusted PRs.

`--host` opts out and runs the agent directly on the host. It's faster, but the
agent has your full credentials and network, so keep it for trusted or local
runs. Bubble needs a working [Incus](https://linuxcontainers.org/incus/) runtime;
if you don't have one, `tauceti doctor` says so and you run with `--host`. You
don't have to install bubble yourself, `tauceti` fetches it with `uvx` when it
isn't already on your `PATH`. On `--host` you can point at a non-default `claude`
with `TAUCETI_CLAUDE_CMD` (a sandbox wrapper, a differently-named build, ...); it's
split as a shell word list and the usual flags are appended.

The agent's conversation transcript is noisy, so by default a round redirects it
to a timestamped file under `logs/` and prints the path (tailing it if the agent
exits non-zero). Pass `--stream` to watch it live on the terminal instead.

## Pacing against quota

`tauceti` paces itself against your subscription quota with no setup. It reads the
credential files the official CLIs already maintain (`~/.claude/.credentials.json`,
`~/.codex/auth.json`) and queries each provider's usage endpoint. The rule is
"keep usage under elapsed time": a provider is available while `used% ≤ elapsed%`
on both its 5-hour and its weekly window. Auto mode prefers Codex (to spare the
scarcer Opus), falls back to Opus, and sleeps when neither is under pace. If it
can't read usage, it treats the provider as unavailable rather than guessing it's
free.

`--ignore-quota` turns the pacer off (then pass an explicit `--agent`).
`--quota-cmd <cmd>` (or `TAUCETI_QUOTA_CMD`) swaps in your own pacer instead: it's
run as `<cmd> <agent>`, and its first line of stdout is the model to run now, or
empty for "wait".

## Inside the sandbox

In a bubble round the checkout, `lake build`, and every git/gh call happen inside
the container:

- GitHub traffic goes through bubble's auth proxy, scoped to
  `FormalFrontier/TauCeti`. A push or API call outside that repo is rejected by
  the proxy, not just flagged by CI later.
- Only the one credential the agent needs is seeded. The other models'
  credentials, and all your host config (`CLAUDE.md`, skills), stay out.
- Review runs the `tauceti-review` engine inside the container too, offline: the
  engine, the roadmap, and the review store are mounted in, and it runs on the
  image's `python3` with no PyPI or cross-repo fetch. The only traffic crossing
  the proxy is the TauCeti clone, the PR API, and the scoreboard post.
- The shared Mathlib cache is an overlay, so one round can't poison a later build,
  and the container is ephemeral.

The sandbox itself lives at [kim-em/bubble](https://github.com/kim-em/bubble).

> OpenRouter agents under bubble need `pi` and openrouter.ai egress in the bubble
> image ([kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299)). Until
> that lands, run `--agent deepseek|minimax` with `--host`.

## Many workers at once

Each worker namespaces its state, checkout, review store, and logs by id, so
several can share a host:

```bash
tauceti work --loop --worker-id alice --isolate-home --only review
tauceti work --loop --worker-id bob   --isolate-home --only roadmap
```

`--worker-id` pins a stable name. `--isolate-home` gives each worker its own
`$HOME` (symlinking your read-only Claude tool surface, copying the mutable auth
in once) so their credential refreshes don't race. The workers coordinate through
GitHub, not through each other: the per-PR scoreboard comment is the shared review
state, `git-safe-push` / `gh-safe-pr-create` compare-and-swap so no one clobbers
another's push, and `claim.sh` hands out branches. Add workers and throughput goes
up.

## What you need

- Always: `gh` (logged in as the account the worker should act as), `git`, `uv`,
  and `jq`.
- Bubble (the default sandbox): a working Incus runtime. `tauceti` fetches the
  bubble CLI itself.
- `--host` authoring: an `elan`/`lake` toolchain on the host.
- The agents you want: `codex` and/or `claude` logged in, and for
  `--agent deepseek|minimax`, `pi` plus an exported `OPENROUTER_API_KEY`.

`tauceti doctor` checks all of this.

## What's in the repo

- `tauceti`: the worker, one Python file ([PEP 723](https://peps.python.org/pep-0723/);
  `uv` resolves its one dependency, `rich`).
- `scripts/`: `claim.sh`, `git-safe-push`, `gh-safe-pr-create`. The agents run
  these on `PATH` inside a round, so they stay shell.
- `prompts/*.md`: the per-task agent prompts.
- `tests/`: `parity_selectors.py`, `lifecycle.sh`, `agent_cmds.py`.
- `checkouts/`, `state/`, `logs/`: runtime only, git-ignored.
