# Inside the sandbox

`tauceti work --bubble` runs the agent inside a [bubble](https://github.com/kim-em/bubble)
container instead of on the host. This page describes what that boundary
actually enforces. The README covers when to use it.

## What happens in the container

In a bubble round the checkout, `lake build`, and every git and gh call happen
inside the container:

- GitHub traffic goes through bubble's auth proxy, scoped to
  `TauCetiProject/TauCeti`. A push or API call outside that repo is rejected by
  the proxy, not just flagged by CI later. Your `gh` token never enters the
  container.
- Only the one credential the agent needs is seeded. The other models'
  credentials, and all of your host config (`CLAUDE.md`, skills), stay out.
- Review runs the `tauceti-review` engine inside the container too, offline. The
  engine, the roadmap, and the review store are mounted in, and it runs on the
  image's `python3` with no PyPI or cross-repo fetch. The only traffic crossing
  the proxy is the TauCeti clone, the PR API, and the scoreboard post.

That isolation matters most for review, where the agent reads untrusted PRs.

## Lake caches

Before the work agent starts, the worker fetches Mathlib's prebuilt outputs with
`lake exe cache get`, fetches TauCeti's own main-built outputs with
`lake cache get`, and runs an advisory `lake build`. A red tree still reaches the
repair agent.

Bubble routes both download-only caches through its host-global proxy, so
TauCeti's public R2 host is never reachable from inside the container. The exact
revision and content-addressed GET routes are shared across rounds; containers
cannot select another origin or upload, and their writable Lake views are
discarded when the container is popped.

## Requirements

Bubble needs a working [Incus](https://linuxcontainers.org/incus/) runtime, and
TauCetiWorker requires Bubble 0.7.29 or newer. Real sandbox rounds need a stable
installed executable, because bubble owns a host-global auth daemon; for dry-run
probes only, `tauceti` will fetch it with `uvx`. `tauceti doctor` reports what is
missing. `TAUCETI_BUBBLE` overrides the executable and `TAUCETI_BUBBLE_HOME` the
private bubble home.

## OpenRouter agents

`--agent deepseek|minimax` runs in the bubble too. The image ships
[`pi`](https://github.com/badlogic/pi-mono) and allows openrouter.ai egress
([kim-em/bubble#299](https://github.com/kim-em/bubble/pull/299)), and
`OPENROUTER_API_KEY` is staged read-only into the container.

## macOS Claude credentials

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file, but bubble seeds the in-container `claude` from a `.credentials.json`.
So `tauceti` copies the current credential out of the Keychain into a private,
transient config directory used only by the bubble subprocess. The Keychain is
read, never written; the first round unlocks it interactively if it is locked.
The directory is removed after the bubble exits, or before the next round if the
worker was hard-killed. Your `$CLAUDE_CONFIG_DIR` (or `~/.claude`) is never
created or overwritten.

Worker versions before this private handoff may already have left a Keychain
snapshot at `.claude/.credentials.json` under your configured Claude directory.
This version does not delete that file, because it may be operator-owned. If
host subscription reviews fail with a 401 while an interactive `claude` still
works, move that old file aside once so the review can fall back to the live
Keychain. A headless worker whose Keychain cannot be unlocked still needs a file
fallback: point `CLAUDE_CONFIG_DIR` at a dedicated directory holding a current
`.credentials.json` instead of moving away its only credential source.
