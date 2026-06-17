# Tau Ceti Worker

`tauceti` is an autonomous worker that keeps the
[TauCeti](https://github.com/FormalFrontier/TauCeti) Lean library moving forward
on your Claude Max / Codex subscriptions. It runs unattended: each round it picks
the single most useful thing to do right now — land a green PR, review an open
one, fix a PR a review flagged, bump Mathlib, or author a new PR against the
roadmap — does exactly that one thing, and sleeps until quota allows the next
round.

It's one self-contained program (`tauceti`, a single Python file run via
[`uv`](https://docs.astral.sh/uv/)). Run it with **no command** for a dashboard
of available work plus a launcher; `tauceti work` does the work; `tauceti status`
prints the same survey for scripts.

> The worker is wired to one deployment: it acts as `kim-em` on
> `FormalFrontier/TauCeti`. The repo/owner are not configurable knobs — this is
> an operator's tool for that project, not a general framework.

## What one round does

Every round performs **one** unit of work, chosen in strict priority order. The
first applicable step wins; the rest wait for a later round.

| # | Step | What it does |
|---|------|--------------|
| 0 | **Housekeeping** | *(always, quota-free)* Merge every PR already green-lit by review (and forward Lake-pin bumps re-validated by `check-bump` at merge time); close PRs that spent their whole review budget without going green; close duplicate roadmap PRs. CI-side merge is intentionally off, so the worker is the only thing that lands or retires PRs. |
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
./tauceti                  # dashboard of available work + a launcher (the default view)
./tauceti status           # the same survey, non-interactive (add --json for scripts)
./tauceti work             # do whatever is most helpful — exactly one round, then exit
./tauceti work --loop      # the driver: round after round, pacing itself against quota
```

`tauceti` is a single file with a [PEP 723](https://peps.python.org/pep-0723/)
header; the `uv` shebang resolves its one dependency (`rich`, for the dashboard)
on first run. Press Ctrl-C to stop the current round and exit. Per-round output
from the loop lands in `logs/<worker-id>/`.

Run `./tauceti doctor` first to check the environment (tools, bubble, quota
credentials); it tells you exactly what's missing and when to pass `--host`.

## Modes

Three independent dials. Leave them at their defaults and the worker does
whatever is most helpful, on whatever subscription has quota. Turn any of them and
you get a focused worker. They combine freely.

### 1. What work — `--only` (default: the whole cascade)

By default a round walks the full priority cascade above and does the first
applicable step. `--only <task>[,<task>...]` confines it to certain kinds of work:

```bash
./tauceti work --loop --only review        # a reviewer: only review open PRs
./tauceti work --loop --only roadmap       # an author: only open new roadmap PRs
./tauceti work --loop --only fix,fix-ci    # a maintainer: only tend to our own PRs
./tauceti work --loop --only bump          # only keep us current with Mathlib master
./tauceti work --loop --only merge         # a janitor: only land/abandon/dedup (no model)
```

Valid tasks are the step names: `rebase`, `review`, `fix`, `fix-ci`, `bump`,
`roadmap`, and `merge`. The **housekeeping pre-passes always run** in every mode
— a focused worker still lands green PRs and clears stuck/duplicate ones — so the
queue stays healthy no matter what each worker is pinned to. `--only merge` is the
special case: it names *housekeeping only*, drives no model, and skips the quota
wait entirely — a cheap janitor to run alongside model-spending workers.

### 2. Which agent — `--agent` (default: auto)

`--agent` is orthogonal to `--only`, so any kind of work can run on any agent:

| `--agent` | Model | Billing |
| --- | --- | --- |
| `auto` *(default)* | Codex preferred (to spare the scarcer Opus), Opus fallback | subscription, paced |
| `codex` | Codex only | subscription, paced |
| `claude` | Opus only | subscription, paced |
| `deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | **pay-per-token** (`OPENROUTER_API_KEY`) |
| `minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | **pay-per-token** (`OPENROUTER_API_KEY`) |

The old `--codex` / `--claude` / `--deepseek` / `--minimax` flags still work as
aliases. A configured default can be set with `TAUCETI_AGENT`. The OpenRouter
agents are pay-per-token, so there is **no auto-dispatch** — they run only when
named (the flag is the budget gate). Override a model id with `DEEPSEEK_MODEL` /
`MINIMAX_MODEL`; point at a non-default `pi` runner with `PI_RUN`.

### 3. Where it runs — bubble by default, `--host` to opt out

The authoring/fixing modes (`rebase`, `fix`, `fix-ci`, `bump`, `roadmap`) run the
work agent inside the [`bubble`](https://github.com/kim-em/bubble) sandbox by
default: a repo-scoped container where the host `kim-em` token never enters
(git/gh go through bubble's auth proxy), only the one credential the work model
needs is seeded, and no host config crosses the boundary. Pass `--host` to opt out
(faster, but the agent has the host's full credentials and network) — for
trusted/local runs.

Bubble needs a working **Incus** runtime; `tauceti doctor` reports whether it's
available, and without it you pass `--host`. See [Sandboxing](#sandboxing) and
[`docs/BUBBLE_TESTING.md`](docs/BUBBLE_TESTING.md).

> **Review sandboxing is being finalized** — see `docs/BUBBLE_TESTING.md` for the
> current state. Run review with `--host` until that lands; the `tauceti-review`
> engine isolates each reviewer itself (its own credential, no host config,
> read-only tools, throwaway HOME).

### Combining

```bash
./tauceti work --loop                       # auto agent, all work
./tauceti work --loop --only roadmap --codex # a Codex-only roadmap author
./tauceti work --loop --only fix --host     # a host-side fixer (no bubble)
./tauceti work --loop --only merge          # a quota-free janitor
./tauceti work --only review --host         # one host-side review round, then exit
```

## Quota: the self-contained pacer

`tauceti` paces itself with **no dependency on any external scripts** — it reads
the standard credential files the official CLIs already maintain
(`~/.claude/.credentials.json`, `~/.codex/auth.json`) and queries each provider's
usage endpoint directly. The rule is "keep usage below elapsed time": a provider
is available only while `used% ≤ elapsed%` on **both** its session (5-hour) and
weekly windows. Auto mode prefers Codex (to spare the scarcer Opus), falls back to
Opus, and sleeps when neither is under pace. Missing or unparseable usage reads as
*unavailable* (fail closed), never as free quota.

`--ignore-quota` disables the gate (then name an explicit `--agent`). `--quota-cmd
<cmd>` (or `TAUCETI_QUOTA_CMD`) replaces the built-in pacer with an external
command — the escape hatch for a custom or multi-account scheme: it's run as
`<cmd> <agent>` and its first stdout token is the model to run now (or empty for
"none available").

## Sandboxing

In a bubble round the checkout, `lake build`, and every `git`/`gh` call happen
**inside** a repo-scoped container:

- **GitHub** — the host token never enters; all traffic goes through bubble's auth
  proxy, scoped to `FormalFrontier/TauCeti`, so a push or API call outside that
  repo is rejected by the proxy, not merely flagged by CI afterward.
- **Credentials** — only the one credential the work model needs is seeded
  (`--codex-credentials` / `--claude-credentials`, or the `OPENROUTER_API_KEY`
  mounted read-only for the OpenRouter agents). The others, and all host config
  (CLAUDE.md, skills), stay out.
- **Isolation** — a worker-private `BUBBLE_HOME` (override via `$TAUCETI_BUBBLE_HOME`)
  and `--local` keep a round from inheriting ambient mounts or a remote/cloud
  default; the shared Mathlib cache is an overlay so one round can't poison a
  later build. The container is `--ephemeral` and explicitly popped on teardown.

Review sandboxing (running `tauceti-review` itself under containment) is in
progress — `docs/BUBBLE_TESTING.md` tracks it; until it lands, `--host` review is
the supported path (the engine has its own read-only-tool clean room).

> OpenRouter agents under bubble need `pi` + openrouter.ai egress in the image
> ([kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299)); until that
> lands, `--agent deepseek|minimax` requires `--host` (it fails early otherwise).

Validating the bubble paths needs an Incus-equipped machine — the checklist is
[`docs/BUBBLE_TESTING.md`](docs/BUBBLE_TESTING.md), with `--host` as the fallback.

## Running many workers on one host

Each worker namespaces its state, checkout, review store, and logs by id, so
copies on one host don't collide:

```bash
./tauceti work --loop --worker-id alice --isolate-home --only review --host
./tauceti work --loop --worker-id bob   --isolate-home --only roadmap
```

`--worker-id` pins a stable name; `--isolate-home` gives each worker its own HOME
(symlinking the read-only Claude tool surface from yours, copying the mutable
auth in once) so their credential refreshes don't race. Coordination between
workers is through GitHub: the per-PR scoreboard comment is the shared review
state, a create-only push arbiter (`git-safe-push` / `gh-safe-pr-create`)
serializes writes, and `claim.sh` provides cooperative de-contention — so adding
workers grows throughput without two of them clobbering each other.

## Prerequisites

On `PATH`, logged in to the subscriptions you want used:

- **Always:** `gh` (as `kim-em`), `git`, `jq`, and `uv`/`uvx`.
- **Reviewing:** the `tauceti-review` engine runs via `uvx`.
- **Sandbox (default for authoring):** [`bubble`](https://github.com/kim-em/bubble)
  and a working Incus runtime; the Lean toolchain lives in the container image.
- **`--host` authoring:** an `elan`/`lake` toolchain on the host.
- **Subscription agents:** `claude` (Claude Code, Opus) and/or `codex` (ChatGPT) on
  the host, logged in.
- **`--agent deepseek|minimax`:** the [`pi`](https://github.com/badlogic/pi-mono)
  agent on `PATH` and `OPENROUTER_API_KEY` **exported**.

`./tauceti doctor` checks all of this and tells you what's missing.

## What lives here

- `tauceti` — the single-file program (the whole worker).
- `claim.sh`, `git-safe-push`, `gh-safe-pr-create` — the agent-facing coordination
  wrappers; the agents invoke these on `PATH` inside a round, so they stay shell.
- `prompts/*.md` — the per-task agent prompts.
- `tests/` — `parity_selectors.py` (the survey's selectors), `lifecycle.sh`
  (lock / signals / timeout / fd handling), `agent_cmds.py` (agent command lines).
- `docs/BUBBLE_TESTING.md` — the checklist for validating the bubble paths on an
  Incus-equipped machine.
- `checkouts/`, `state/`, `logs/` are runtime-only and git-ignored.
