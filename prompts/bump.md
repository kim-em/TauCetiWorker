You are adapting FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib, to a Mathlib bump on pull request #__PR__. You are in a checkout of the repo, already on the PR's branch. A bot opened this PR to move the Lake pins (`lake-manifest.json` and/or `lean-toolchain`) forward to a newer Mathlib, and the `build` check is red because `TauCeti/` has not caught up to the Mathlib API at the new pin. Work autonomously to completion: make CI green by adapting `TauCeti/`, without reverting the bump and without weakening the library.

## The pins are the point — keep them
- The bumped `lake-manifest.json` / `lean-toolchain` on this branch ARE the change under review. Do NOT revert them, do NOT re-pin to an older Mathlib, do NOT touch the lakefile. Your job is to make `TauCeti/` build against the Mathlib the bot pinned.
- If the new pin is genuinely unworkable (e.g. a Mathlib change that can't be adapted without a redesign), stop and report that, rather than reverting the bump or gutting the library.

## Reproduce and adapt
```
lake exe cache get
lake build
lake exe axioms
```
- Read the build failures. The usual cause is a renamed/moved/retyped Mathlib lemma or a changed signature. Fix each by updating the `TauCeti/` proof or statement to the new Mathlib API. Prefer the smallest correct change.
- For a failing check's logs: `gh pr checks __PR__ --repo FormalFrontier/TauCeti`, then `gh run view <run-id> --repo FormalFrontier/TauCeti --log-failed`.
- If the failure is genuinely transient infra (e.g. a cache fetch timeout) and the code builds clean locally, push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so.

## Rules of the repo (hard constraints)
- Adapt code under `TauCeti/`. Do NOT edit the root `TauCeti.lean`: it is regenerated and committed automatically on `main` after merge. The only files outside `TauCeti/` you may leave changed are the pins the bot already bumped. Do NOT touch `Scripts/`, `.github/`, or the lakefile (`lakefile.toml`/`lakefile.lean`).
- Everything under `namespace TauCeti`.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter (e.g. with `set_option ... false`) to force the build green.

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red.

**Do this synchronously, in this one turn.** Run these commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed and pushed (below). Pushing is the only thing that preserves your work.

## Submit
- Commit the adaptation (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  git-safe-push
  ```
  This compare-and-swaps the PR branch against the head you started from, so a concurrent agent's work is never silently clobbered. Do NOT run a raw `git push` (nor `--force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so; do not work around it. A successful push updates the PR; CI re-runs automatically.
- Do NOT open a new PR; do NOT touch files outside `TauCeti/` (and the already-bumped pins).

## Report
End with a concise summary: which Mathlib changes broke `TauCeti/`, how you adapted each, and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
