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

## Run

```bash
./loop.sh            # runs forever; Ctrl-C stops the current round and exits
```

Each round runs under a 90-minute hard timeout in its own process group, so a
wedged sub-task is torn down rather than parking the loop. Per-round output goes
to `logs/<task>-<timestamp>.log`.

## Prerequisites

On `PATH`, logged in to the subscriptions you want used:

- `gh` (as `kim-em`), `git`, `jq`, `uv`/`uvx`, and an `elan`/`lake` toolchain.
- `claude` (Claude Code, Opus subscription) and/or `codex` (ChatGPT subscription).
- The `claude-usage` skill scripts at `~/.claude/skills/claude-usage/`.

## How it decides (round.sh)

- "Needs review" / "needs fix" are read from the `tauceti-review` persistent
  store ledger (`~/.cache/tauceti-review/store/FormalFrontier__TauCeti/ledger.json`):
  a PR needs review when its current head isn't the last reviewed head; it needs
  a fix when the latest round at the current head has a `blocking_request` /
  `blocking_block` rubric. A fix is retried at most 3× per head (`state/fix-*`).
- Authoring/fixing runs in a reused checkout at `checkouts/TauCeti` (cleaned to
  `origin/main` each round, `.lake` preserved for fast builds).
- The agent is driven by the prompt templates in `prompts/` (`__PR__` / `__AVOID__`
  are substituted per round). Agents run with full tool access on the
  subscription (Claude with `--dangerously-skip-permissions` and
  `ANTHROPIC_API_KEY` unset so it bills the Max plan; Codex with
  `--sandbox danger-full-access`).

## Safety notes

- The agents run with broad permissions on your machine and push to GitHub as
  `kim-em`. They are constrained by the prompts (TauCeti/-only, no linter
  silencing, build-green-before-push) but **not sandboxed** — run this only where
  you are comfortable with that. Sandboxing them (via `bubble`, with a scoped
  GitHub token) is the top item in [TODO.md](TODO.md).
- `loop.sh` runs a `preflight` at startup (checks `gh`/`git`/`jq`/`uvx`/`lake`,
  at least one of `claude`/`codex`, the quota scripts, and `gh auth`) and exits
  loudly if anything is missing. A GitHub API failure mid-round aborts that round
  rather than silently falling through to authoring.
- `checkouts/`, `state/`, and `logs/` are runtime-only and git-ignored.

See [TODO.md](TODO.md) for the planned sandbox and a third (DeepSeek via
OpenRouter+Pi) reviewer-author.
