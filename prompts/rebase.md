You are resolving merge conflicts on pull request #__PR__ of FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The PR has become un-mergeable: its branch conflicts with the current `main`. Bring it up to date with `main` and resolve the conflicts so it can merge again. Work autonomously to completion.

## Rebase onto current main
- Fetch and integrate the latest `main`:
  ```
  git fetch origin
  git merge origin/main      # (or: git rebase origin/main — either is fine; merge is simpler to resolve)
  ```
- Resolve every conflict on its merits:
  - **`TauCeti.lean` (the root import list)** is the usual culprit: several PRs each add an `import TauCeti.…` line, so they collide. Resolve by taking the UNION of both sides' imports — keep every import from `main` AND your PR's new one — and keep the list alphabetically ordered. Never drop an existing import.
  - **A source file under `TauCeti/`**: resolve so both the upstream change and your PR's intent are preserved. If `main` now provides something your PR duplicated, prefer the upstream version and drop the duplicate.
- Do NOT discard upstream work to "win" a conflict, and do NOT weaken or delete your PR's real content to dodge one. If a conflict is genuinely irreconcilable (your PR's target no longer makes sense because `main` subsumed it), stop and say so in your report rather than forcing a merge.

## Rules of the repo (hard constraints)
- Code goes under `TauCeti/`. You MAY edit the root `TauCeti.lean` (that is expected here). Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Everything under `namespace TauCeti`.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and never silence a linter.

## Verify before pushing (all three MUST pass, after the merge/rebase)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red — a botched conflict resolution that builds red is worse than the conflict.

## Submit
- Commit the merge/resolution (if `git merge` left a merge commit, keep its default message; otherwise `<type>: <subject>`, ending the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push with the project's safe wrapper — and ONLY the wrapper:
  ```
  git-safe-push
  ```
  It compare-and-swaps the PR branch against the head you started from (so it works whether you merged or rebased, and never clobbers a concurrent push). Do NOT run a raw `git push` (nor `git push --force` / `--force-with-lease`); the wrapper is the only sanctioned push. If it reports the branch moved or the lease was lost, another agent pushed — STOP and say so in your report; do not work around it.
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which files conflicted, how you resolved each (for `TauCeti.lean`, confirm you unioned the imports), and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
