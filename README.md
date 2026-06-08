# Tau Ceti Worker

An autonomous loop that does Tau Ceti work on your Claude Max / Codex
subscriptions, modelled on `../lean-eval-knill`'s pipeline. Each round does
**one** unit of work, chosen in priority order:

1. **Review** — an open [TauCeti](https://github.com/FormalFrontier/TauCeti) PR
   whose CI build is green and whose current head hasn't been reviewed yet, via
   the `tauceti-review` CLI.
2. **Fix** — one of `kim-em`'s open PRs whose latest review (on the current head)
   requests changes or blocks: an agent reads the findings and either fixes the
   code or contests a wrong finding on its thread.
3. **Roadmap** — otherwise, an agent starts a new PR advancing a roadmap target,
   avoiding the area of the most recently opened PR.

## Quota policy

A round runs only while subscription quota is available, preferring **Codex** to
spare the more precious **Opus** quota; it sleeps and re-checks otherwise. Sonnet
alone does **not** count — the worker wants Opus or Codex. (Uses the
`claude-available-model` / `codex-available-model` scripts from the
`claude-usage` skill.) Authoring and fixing use the preferred available model;
review uses every model that currently has quota (so it stays dual-model when it
can).

## Choosing the model

By default the worker uses your subscriptions (Codex/Opus) as above. An explicit
override flag — passed to `loop.sh` (and forwarded to `round.sh`) — pins **both**
authoring/fixing and reviewing to one model:

| Flag | Model | Quota / billing |
| --- | --- | --- |
| _(none)_ | Codex preferred, Opus fallback; review uses all available | subscription |
| `--codex` | Codex (`gpt-5.5`) only | subscription (waits on Codex quota) |
| `--claude` | Opus only | subscription (waits on Opus quota) |
| `--deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | **pay-per-token** (`OPENROUTER_API_KEY`) |
| `--minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | **pay-per-token** (`OPENROUTER_API_KEY`) |

The OpenRouter models (DeepSeek, MiniMax) are driven through the `pi` agentic
loop — `pi` runs arbitrary models that Claude Code / Codex can't drive natively.
They are **pay-per-token, not a flat subscription, so there is no auto-dispatch**:
they run *only* when you pass their flag (the flag is the budget gate). The
subscription path never reaches for them on its own. Override a model id with the
`DEEPSEEK_MODEL` / `MINIMAX_MODEL` env vars; point at a non-default `pi` runner
with `PI_RUN`. Adding another OpenRouter model is one entry in the
`OPENROUTER_MODELS` map in `round.sh` (and the matching one in `review.py`).

> **Model choice.** `deepseek/deepseek-v4-pro` and `minimax/minimax-m3` are each
> provider's strongest agentic, tool-using model on OpenRouter (both with tool
> use, reasoning, and a 1M-token context). DeepSeek-Prover-V2 and ByteDance
> Seed-Prover top the [Lean eval leaderboard](https://lean-lang.org/eval) but are
> **whole-proof search systems, not tool-using agents** — they prove a given
> statement, they don't review PRs or author library code — and neither is served
> on OpenRouter, so neither can drive `pi`. Hence the general flagship models.

The model flag is independent of `--bubble` (below) and combines with it.

## Run

```bash
./loop.sh                 # subscription auto: Codex preferred, Opus fallback (host)
./loop.sh --codex         # force Codex only
./loop.sh --claude        # force Opus only
./loop.sh --deepseek      # force DeepSeek (OpenRouter + pi; needs OPENROUTER_API_KEY)
./loop.sh --minimax       # force MiniMax M3 (OpenRouter + pi)
./loop.sh --bubble        # sandbox each authoring/fixing round in a container
./loop.sh --bubble --codex   # combine: sandboxed, Codex only
# Ctrl-C stops the current round and exits.
```

A single round can also be run directly: `./round.sh --deepseek` (or with
`--bubble`).

Each round runs under a 90-minute hard timeout in its own process group, so a
wedged sub-task is torn down rather than parking the loop. Per-round output goes
to `logs/<task>-<timestamp>.log`.

## Prerequisites

On `PATH`, logged in to the subscriptions you want used:

- Always: `gh` (as `kim-em`), `git`, `jq`, and `uv`/`uvx`.
- Host authoring (the default): an `elan`/`lake` toolchain on the host (the
  per-round build runs there).
- `--bubble` (sandboxed authoring): [`bubble`](https://github.com/kim-em/bubble)
  and a working Incus runtime — the Lean toolchain then lives in the container
  image, not on the host.
- Subscription models: `claude` (Claude Code, Opus) and/or `codex` (ChatGPT) on
  the host (bubble also bakes them into the image and seeds their credentials),
  plus the `claude-usage` skill scripts at `~/.claude/skills/claude-usage/`.
- `--deepseek` / `--minimax`: the [`pi`](https://github.com/badlogic/pi-mono)
  agent on `PATH` (the `pi` skill wrappers at `~/.claude/skills/pi/`) and
  `OPENROUTER_API_KEY` **exported** — it lives in `~/.zshrc`, which a
  non-interactive shell does not source, so export it before launching the loop.
  (`pi` is required on the host even with `--bubble` — bubble bakes it into the
  container image when present, exactly as it does `claude` / `codex`.)

## How it decides (round.sh)

- "Needs review" / "needs fix" are read from the `tauceti-review` persistent
  store ledger (`~/.cache/tauceti-review/store/FormalFrontier__TauCeti/ledger.json`):
  a PR needs review when its current head isn't the last reviewed head; it needs
  a fix when the latest round at the current head has a `blocking_request` /
  `blocking_block` rubric. A fix is retried at most 3× per head (`state/fix-*`).
- Authoring/fixing runs **on the host by default** (a reused checkout at
  `checkouts/TauCeti`, cleaned to `origin/main` each round, `.lake` preserved for
  fast builds), or **inside a `bubble` container** when `--bubble` is passed (see
  Sandboxing). Review rounds always run on the host (the `tauceti-review` CLI has
  its own clean room).
- The agent is driven by the prompt templates in `prompts/` (`__PR__` / `__AVOID__`
  and, for roadmap rounds, `__ROADMAP_DIR__` / `__REVIEW_DIR__` are substituted
  per round). Agents run with full tool access (Claude with
  `--dangerously-skip-permissions` and `ANTHROPIC_API_KEY` unset so it bills the
  Max plan; Codex with `--sandbox danger-full-access`; DeepSeek/MiniMax through
  the `pi` runner against OpenRouter, billed per-token).

## Sandboxing (`--bubble`, optional)

By default authoring/fixing runs on the host: fast and simple, but the agent has
the host's full git/gh credentials and network. Pass `--bubble` to run each
authoring/fixing round inside a [`bubble`](https://github.com/kim-em/bubble)
container instead, so a misbehaving or prompt-injected agent is bounded by the
container, not the host:

- **Filesystem** — the agent only sees the in-container checkout plus the
  read-only reference mounts the round stages for it (the prompt at `/opt/round`,
  and for roadmap rounds the `TauCetiRoadmap` / `TauCetiReview` clones at
  `/opt/roadmap` / `/opt/review`). It cannot read host files outside the
  workspace, including `~/.claude/CLAUDE.md` or other repositories.
- **GitHub** — the container never sees the host `kim-em` token. All `git`/`gh`
  traffic goes through bubble's auth proxy, repo-scoped to `FormalFrontier/TauCeti`
  — a push or API call outside that repo is rejected by the proxy, not merely
  flagged by CI after the fact. The public reference repos are staged on the host
  and mounted read-only rather than fetched through the proxy; Mathlib is the
  checkout's own vendored Lake dependency.
- **Credentials** — only the one credential the work model needs is seeded:
  `~/.claude/.credentials.json` *or* `~/.codex/auth.json` for the subscriptions,
  or — for `--deepseek` / `--minimax` — the `OPENROUTER_API_KEY` mounted read-only
  at `/opt/round/openrouter.key` (OpenRouter has no proxy, so its key must enter
  the container; the agent can spend against it, exactly as it would on the host).
  The other models' credentials and all host config (CLAUDE.md, skills, Codex
  config) stay out.
- **Isolation from operator config** — the worker drives bubble with a private
  `BUBBLE_HOME` (`~/.cache/tauceti-worker/bubble`, override via
  `$TAUCETI_BUBBLE_HOME`) and `--local`, so a round can't inherit ambient
  `[[mounts]]` or a remote/cloud default from your `~/.bubble/config.toml`. The
  shared Mathlib cache is set to `overlay` (read-only base + per-round writable
  overlay) so one round can't poison a later round's build. First use builds the
  worker's git mirrors and cache there — slow once, fast afterwards.
- **Teardown** — the container is `--ephemeral` (popped when the round's command
  exits, propagating its exit code), and popped again explicitly in case that
  failed; a leftover from a SIGKILLed round is cleared at the start of the next
  round. `round.sh` also takes a `flock` so two rounds can't run at once.

> **`--bubble` with `--deepseek` / `--minimax`** needs the `pi` tool in the
> bubble image (which also allowlists `openrouter.ai` egress); that lands in
> [kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299). Until then,
> sandboxed OpenRouter rounds fail at `pi: command not found`; run them on the
> host (no `--bubble`) in the meantime. `--bubble` with `--codex` / `--claude`
> works today.

Review rounds are unchanged: the `tauceti-review` CLI already runs each reviewer
model in a clean room (only its own credential, no host config) and only
reads/greps — it never builds or pushes.

## Other notes

- `loop.sh` runs a `preflight` at startup (checks `gh`/`git`/`jq`/`uvx`, plus
  `lake` for host authoring or `bubble` for `--bubble`, the relevant model CLI /
  `pi` + `OPENROUTER_API_KEY`, the quota scripts, and `gh auth`) and exits loudly
  if anything is missing. A GitHub API failure mid-round aborts that round rather
  than silently falling through to authoring.
- `checkouts/`, `state/`, and `logs/` are runtime-only and git-ignored.

Reviewing with DeepSeek/MiniMax also needs the `tauceti-review` engine to know
the provider; that landed in
[TauCetiReview#42](https://github.com/FormalFrontier/TauCetiReview/pull/42) (a
`run_pi` OpenRouter reviewer, `--reviewer deepseek|minimax`).
