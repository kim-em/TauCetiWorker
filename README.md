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

Bare `tauceti` opens an interactive dashboard ([Textual](https://textual.textualize.io/)).
The table lists each kind of work with a number, how many PRs are ready, and a
sample; a cursor highlights one row. The survey is fetched once in the background
and refreshed on `r` (or every 90s), so moving the cursor is instant — it never
re-queries GitHub. It reacts to single keypresses (no Enter):

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `k` / `j`) | move the cursor between kinds |
| `→` / `←` | expand / collapse the selected kind — list its PRs with titles (or, on `roadmap`, the focus areas) |
| `Enter` | run one round of the selected kind |
| `1`–`6` | run one round of that numbered kind directly |
| `l` / `L` | loop the auto cascade / loop just the selected kind |
| `f` | pick the roadmap focus from the available areas |
| `m` / `s` | cycle the agent / toggle the sandbox (bubble ↔ host) |
| `r` / `c` / `q` | refresh / copy the launch command to the clipboard / quit |

The agent, sandbox, and roadmap focus you pick are remembered between runs in
`$XDG_CONFIG_HOME/tauceti/dashboard.json` (default `~/.config/tauceti/`), so the
dashboard reopens on your last selections. An explicit `TAUCETI_ROADMAP_FOCUS` in
the environment still overrides the saved focus. The file is a user config dir, not
the per-worker `state/`, so it is shared whether you run `./tauceti` from a clone or
the `uv tool install`ed `tauceti`, and survives upgrades.

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
`~/.codex/auth.json`) and queries each provider's usage endpoint. It honors
`$CLAUDE_CONFIG_DIR` for the Claude credentials, so personal/work account switching
is paced correctly; bubble honors the same var, so it seeds the matching credentials
into the sandbox too. On macOS, where Claude Code keeps its creds in the login Keychain
rather than a file, the pacer reads them from the Keychain instead, read-only: it
never refreshes the Keychain (that would log out your interactive `claude`), so on
token expiry it just reports Claude unavailable for the cycle, and your next `claude`
run (interactive, or one `--ignore-quota --agent claude` round) refreshes the
Keychain so the pacer can read it again. A locked Keychain (headless/SSH) reports
unavailable with a hint to `security unlock-keychain` first. The rule is
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
The OpenRouter agents (`--agent deepseek|minimax`) run in the bubble too: the
image ships [`pi`](https://github.com/badlogic/pi-mono) and allows openrouter.ai
egress ([kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299)), and the
key is staged read-only into the container.

## Many workers at once

Each worker namespaces its state, checkout, review store, and logs by id, so
several can share a host:

```bash
tauceti work --loop --worker-id alice --only review
tauceti work --loop --worker-id bob   --only roadmap
```

`--worker-id` pins a stable name and is the only knob you need: any id other than
`default` also gives that worker its own `$HOME` (symlinking your read-only Claude
tool surface, copying the mutable auth in once) so their credential refreshes don't
race. Your `gh` and `git` config stays shared, not isolated (it doesn't refresh-race,
and the host survey and pushes need it), so the worker still authenticates as you.
(`--isolate-home` still exists, but only to force that same isolation for the
`default` id; a distinct id already implies it.) The workers coordinate through
GitHub, not through each other: the per-PR scoreboard comment is the shared review
state, `git-safe-push` / `gh-safe-pr-create` compare-and-swap so no one clobbers
another's push, and `claim.sh` hands out branches. Add workers and throughput goes
up.

On macOS, Claude Code keeps its creds in the login Keychain rather than a file.
Bubble rounds still work: bubble seeds the in-container `claude` from a
`.credentials.json`, so when one isn't present `tauceti` writes the credential into
your `$CLAUDE_CONFIG_DIR` (or `~/.claude`) from the Keychain, refreshing it when it's
missing or expired (read-only on the Keychain, which is never written; the first
round unlocks it interactively if it's locked). The pacer reads the Keychain
directly, so it never refreshes that file and never rotates the shared login token.
Host rounds, by contrast, share the one per-login-user Keychain, so `--isolate-home`
can't give a host worker its own Claude account there; host-mode multi-worker
isolation on macOS applies to Codex only.

## `tauceti work` reference

`tauceti work` does one round and exits; `--loop` runs the driver. The same list
is in `tauceti work -h`.

| Flag | What it does |
| --- | --- |
| `--loop` | Run the driver: keep doing rounds, pacing against quota between them, instead of one. |
| `--only TASKS` | Restrict the round to a comma list of `rebase,review,fix-ci,fix,bump,roadmap` (default: the whole cascade). |
| `--agent AGENT` | `auto` (default), `codex`, `claude`, `deepseek`, or `minimax` — see the agent table above. |
| `--host` | Opt out of the bubble sandbox and run the agent directly on the host. |
| `--stream` | Stream the agent's log to the terminal instead of a file under `logs/`. |
| `--roadmap-focus AREA` | The single roadmap area for roadmap rounds (empty = all areas). |
| `--ignore-quota` | Skip the pacer (needs an explicit `--agent codex\|claude`). |
| `--quota-cmd CMD` | External pacer, run as `<cmd> <agent>`: first stdout token = model to run, empty output or nonzero exit = wait. |
| `--worker-id ID` | Run an independent worker under this name; any id but `default` also isolates its `$HOME`. |
| `--isolate-home` | Force the per-worker `$HOME` even for the `default` id (a distinct id already implies it). |
| `--dry-run` | Survey and print the picker's decision; act on nothing. |

### Environment variables

Flags win over these. Most are tuning knobs with sane defaults; you rarely set them.

| Variable | Default | Effect |
| --- | --- | --- |
| `TAUCETI_AGENT` | `auto` | Default for `--agent`. |
| `TAUCETI_WORKER_ID` | `default` | Default for `--worker-id`. |
| `TAUCETI_ROADMAP_FOCUS` | `ReductiveGroups` | Default for `--roadmap-focus` (`""` = all areas). |
| `TAUCETI_QUOTA_CMD` | — | Default for `--quota-cmd`. |
| `TAUCETI_STREAM` | — | `1` is the same as `--stream`. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude config/credential dir the pacer and bubble seeding use (account switching, where the creds live in a file). |
| `TAUCETI_CLAUDE_CMD` | `claude` | The `claude` executable for `--host` rounds; split as a shell word list, the usual flags appended. |
| `TAUCETI_CODEX_MODEL` | host's configured model | The Codex model to run in the bubble. |
| `DEEPSEEK_MODEL` / `MINIMAX_MODEL` | `deepseek/deepseek-v4-pro` / `minimax/minimax-m3` | OpenRouter model ids for those agents. |
| `OPENROUTER_API_KEY` | — | Required for `--agent deepseek\|minimax`; staged read-only into the bubble. |
| `PI_RUN` | `~/.claude/skills/pi/scripts/run.sh` | The `pi` runner for OpenRouter agents on `--host`. |
| `TAUCETI_BUBBLE` | `bubble` (else `uvx`-fetched) | Override the bubble executable. |
| `TAUCETI_BUBBLE_HOME` | per-worker cache dir | Override the private bubble home. |
| `TAUCETI_REVIEW_ENGINE_DIR` | — | Use a local `tauceti-review` checkout instead of fetching the engine. |
| `TAUCETI_POLL` | `300` | Seconds between quota checks while the loop waits. |
| `TAUCETI_ROUND_TIMEOUT` | `5400` | Hard cap per round (seconds). |
| `TAUCETI_INTERROUND` | `20` | Minimum gap after a productive round (seconds). |
| `TAUCETI_BACKOFF_BASE` / `TAUCETI_BACKOFF_MAX` | `30` / `900` | The escalating no-progress back-off (seconds). |
| `TAUCETI_META_TTL` | `120` | How long a cached scoreboard stays fresh (seconds). |
| `CLAIM_TTL` / `CLAIM_HEARTBEAT` | `1500` / `300` | Branch-claim lease TTL and heartbeat interval (seconds). |

## What you need

- Always: `gh` (logged in as the account the worker should act as), `git`, `uv`,
  and `jq`.
- Bubble (the default sandbox): a working Incus runtime. `tauceti` fetches the
  bubble CLI itself.
- `--host` authoring: an `elan`/`lake` toolchain on the host.
- The agents you want: `codex` and/or `claude` logged in, and for
  `--agent deepseek|minimax`, an exported `OPENROUTER_API_KEY` (`pi` ships in the
  bubble image; you only need it on the host for `--host` rounds).

`tauceti doctor` checks all of this.

## What's in the repo

- `tauceti`: the worker, one Python file ([PEP 723](https://peps.python.org/pep-0723/);
  `uv` resolves its dependencies, `rich` and `textual`, both used only by the dashboard).
- `scripts/`: `claim.sh`, `git-safe-push`, `gh-safe-pr-create`. The agents run
  these on `PATH` inside a round, so they stay shell.
- `prompts/*.md`: the per-task agent prompts.
- `tests/`: `parity_selectors.py`, `lifecycle.sh`, `agent_cmds.py`, `claude_config_dir.py`,
  `claude_keychain.py`.
- `checkouts/`, `state/`, `logs/`: runtime only, git-ignored.
