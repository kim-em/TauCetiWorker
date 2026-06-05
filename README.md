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

- `gh` (as `kim-em`), `git`, `jq`, and `uv`/`uvx`.
- [`bubble`](https://github.com/kim-em/bubble) and a working Incus runtime — each
  authoring/fixing round runs in a container, so the Lean toolchain (`elan`/`lake`)
  lives in the container image, not on the host.
- `claude` (Claude Code, Opus subscription) and/or `codex` (ChatGPT subscription),
  installed on the host so bubble bakes them into the container image and can seed
  their subscription credentials.
- The `claude-usage` skill scripts at `~/.claude/skills/claude-usage/`.

## How it decides (round.sh)

- "Needs review" / "needs fix" are read from the `tauceti-review` persistent
  store ledger (`~/.cache/tauceti-review/store/FormalFrontier__TauCeti/ledger.json`):
  a PR needs review when its current head isn't the last reviewed head; it needs
  a fix when the latest round at the current head has a `blocking_request` /
  `blocking_block` rubric. A fix is retried at most 3× per head (`state/fix-*`).
- Authoring/fixing runs inside a fresh `bubble` container (see Sandboxing below):
  the per-round checkout, `lake exe cache get` / `lake build` / `lake exe axioms`,
  and all `git`/`gh` happen in the container, not on the host.
- The agent is driven by the prompt templates in `prompts/` (`__PR__` / `__AVOID__`
  are substituted per round; the filled prompt is handed to the container on a
  read-only mount). Inside the sandbox the agent still runs with full tool access
  on the subscription (Claude with `--dangerously-skip-permissions` and
  `ANTHROPIC_API_KEY` unset so it bills the Max plan; Codex with
  `--sandbox danger-full-access`) — that "full access" is now bounded by the
  container.

## Sandboxing

Each authoring/fixing round runs inside a
[`bubble`](https://github.com/kim-em/bubble) container, so a misbehaving or
prompt-injected agent is bounded by the container, not the host:

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
- **Credentials** — only the one subscription credential the work model needs
  (`~/.claude/.credentials.json` *or* `~/.codex/auth.json`) is seeded into the
  container; the other subscription and all host config (CLAUDE.md, skills, Codex
  config) stay out. The agent can necessarily read — and in principle exfiltrate —
  the single subscription credential it runs under (exactly as the review clean
  room can); that residual is accepted. The *other* subscription and the host
  GitHub token remain out of reach.
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

The agent still runs `--dangerously-skip-permissions` / `--sandbox
danger-full-access` *inside* the container — that "full access" is now the
container's, which is the point. The prompts' constraints (TauCeti/-only, no
linter silencing, build-green-before-push) remain as guidance; the enforcement
boundary is the container and the repo-scoped proxy.

Review rounds are unchanged: the `tauceti-review` CLI already runs each reviewer
model in a clean room (only `.credentials.json` copied, no host config) and only
reads/greps — it never builds or pushes.

## Other notes

- `loop.sh` runs a `preflight` at startup (checks `gh`/`git`/`jq`/`uvx`/`bubble`,
  at least one of `claude`/`codex`, the quota scripts, and `gh auth`) and exits
  loudly if anything is missing. A GitHub API failure mid-round aborts that round
  rather than silently falling through to authoring.
- `state/` and `logs/` are runtime-only and git-ignored.

Planned next: [#2 a third (DeepSeek via OpenRouter+Pi) reviewer-author](https://github.com/kim-em/TauCetiWorker/issues/2).
