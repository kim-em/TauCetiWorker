# TauCetiWorker — TODO

## 1. Sandbox the agents (high priority)

Right now each round runs the authoring/fixing agent directly on the host with
**full access** — `claude --dangerously-skip-permissions` and
`codex --sandbox danger-full-access` (`round.sh`, `run_agent`) — and it pushes to
GitHub as `kim-em` with that account's full token. A misbehaving or
prompt-injected agent could touch anything the user can, and push anywhere. The
prompts ("only `TauCeti/`", "build green before push") are guidance, **not an
enforcement boundary**; the only real downstream guard is CI's `TauCeti/`-scope
check, which merely fails the build after the fact.

**TODO:** run each round inside a sandbox.

- Use [`kim-em/bubble`](https://github.com/kim-em/bubble) (containerized Lean 4
  dev environments) — see how [`FormalFrontier/pod`](https://github.com/FormalFrontier/pod)
  drives `bubble`. The per-round checkout, `lake exe cache get` / `lake build` /
  `lake exe axioms`, and the `git`/`gh` calls should all happen **inside** the
  container, not on the host.
- Give the container a **scoped** GitHub credential — a repo-scoped token (or the
  tauceti-review App installation token) limited to `FormalFrontier/TauCeti` (and
  `TauCetiRoadmap` read), **not** the full `kim-em` PAT.
- Keep `~/.claude` / `~/.codex` subscription credentials out of the container
  except the minimum needed to run `claude -p` / `codex exec` (mirror the
  clean-room seeding the `tauceti-review` CLI already does for review).
- Acceptance: an agent that goes rogue cannot read host files outside the
  workspace, cannot push outside `TauCeti/`, and cannot exfiltrate the host
  GitHub token or other subscriptions.

This is the technical guardrail behind the README's "not sandboxed — run only
where you're comfortable" caveat; that caveat goes away once this is done.

## 2. Add DeepSeek as a third reviewer/author (via OpenRouter + Pi)

Try doing all three jobs — reviewing, opening PRs, and responding to reviews —
with the **best DeepSeek model for maths/Lean**, alongside Claude Opus and Codex.

- **Pick the model:** check the [Lean eval leaderboard](https://lean-lang.org/eval)
  results and choose the strongest DeepSeek entry (candidates to compare:
  DeepSeek-V3.x, DeepSeek-R1, DeepSeek-Prover-V2). Record the chosen OpenRouter
  model id.
- **Driver:** use the `pi` coding agent (the `pi` skill at
  `~/.claude/skills/pi/`, wrappers in `scripts/`) via OpenRouter
  (`OPENROUTER_API_KEY`) — `pi` runs an agentic loop with arbitrary models that
  Claude Code/Codex can't drive natively.
- **Authoring/fixing:** add a `WORK_MODEL=deepseek` path in `round.sh`'s
  `run_agent` that shells to the `pi` wrapper with the chosen model and the same
  `prompts/*.md`.
- **Reviewing:** extend the `tauceti-review` engine
  ([TauCetiReview](https://github.com/FormalFrontier/TauCetiReview)
  `runner/review.py`) with a third provider (a `run_pi`/`run_openrouter`
  alongside `run_claude`/`run_codex`), and let `--reviewer`/`--providers` include
  `deepseek`. Then the worker can review with whichever of the three has budget.
- **Quota/budget gate:** OpenRouter is pay-per-token (not a flat subscription),
  so unlike Codex/Opus there's no `*-available-model` script — gate it on a
  spend budget instead (or just an env flag to enable it).
- Worth measuring head-to-head: does DeepSeek catch defects the Claude/Codex
  reviewers miss, and how do its authored PRs fare under review?
