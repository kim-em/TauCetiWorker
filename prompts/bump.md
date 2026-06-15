You are bumping FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib, up to the current tip of Mathlib's `master` and fixing whatever breaks. You are in a checkout of the repo. Mathlib `master` has advanced past our pin; bring TauCeti up to it, green and axiom-clean, in one focused PR. Work autonomously to completion.

The Mathlib commit that prompted this round is `__TARGET__`, but `master` may have advanced further since — that is fine: bump to whatever `lake update` resolves (it is still a forward move, which is what the CI `bump-guard` check validates).

## Start from a clean main and branch
```
git fetch origin
git checkout main && git reset --hard origin/main
git checkout -b __BRANCH__
```
Use exactly this branch name (`__BRANCH__`). It is deterministic so two workers attempting the same bump collide on the create-only push and only one wins; do not invent a different name.

## Do the bump
- Pull Mathlib (and its transitive deps) to `master`, which rewrites `lake-manifest.json`:
  ```
  lake update mathlib
  ```
- Match the Lean toolchain to the one Mathlib now requires (they MUST agree), by copying Mathlib's:
  ```
  cp .lake/packages/mathlib/lean-toolchain lean-toolchain
  ```
- Fetch the prebuilt oleans and build:
  ```
  lake exe cache get
  lake build
  ```
- `lake build` will surface the breakages caused by Mathlib changes between our old pin and `master`. Fix them **in `TauCeti/` only**, one file at a time, until the whole library builds. Typical breakages: renamed/relocated lemmas, deprecated names, structure-field renames, and tactic-behaviour changes (e.g. `convert`/`simp`). Prefer the smallest correct adaptation; when a lemma moved, add the right `import` and use the new name. The pinned Mathlib source is vendored at `.lake/packages/mathlib` — `grep` it to find the new name/location of anything that moved.

## Hard rules of the repo
- You may change ONLY: files under `TauCeti/`, the root `TauCeti.lean` (imports; keep it alphabetical), `lean-toolchain`, and `lake-manifest.json`. Do NOT touch `lakefile.toml`/`lakefile.lean`, `Scripts/`, or `.github/` — those are human-owned and would force this out of the auto-merge path (and `bump-guard` fails if the lakefile changed or a pin moved backward).
- Do NOT move a pin backward and do NOT change which branch the lakefile nominates; this is a forward bump only.
- Everything stays under `namespace TauCeti`. Never weaken or silence a linter, never add `maxHeartbeats` overrides.
- Must build green AND pass the axiom audit (allowlist: `propext`, `Classical.choice`, `Quot.sound`; no `sorry`/`native_decide`/new axioms).

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
If `lake build` is red, keep fixing — never push red. If the bump turns out to need changes outside the files you're allowed to touch (e.g. a genuine lakefile change), STOP and say so in your report rather than forcing it; a human will take that one.

## Submit
- Commit. Use message `chore: bump mathlib to master and lean toolchain to <ver>` (fill `<ver>` with the new `lean-toolchain`); the body should summarize the Mathlib changes you adapted to. End the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`.
- Push the new branch with the project's safe wrapper — and ONLY the wrapper:
  ```
  git-safe-push __BRANCH__
  ```
  This create-only-pushes the branch (it fails closed if `__BRANCH__` already exists, so two agents can't collide). If it reports the branch already exists or the lease was lost, another worker is doing this bump — STOP and say so; do not work around it. Do NOT run a raw `git push`.
- Open the PR with the project's safe wrapper — and ONLY the wrapper:
  ```
  gh-safe-pr-create --repo FormalFrontier/TauCeti --base main --title "chore: bump mathlib to master and lean toolchain to <ver>" --body-file <file>
  ```
  Do NOT run a raw `gh pr create`. The body opens with a paragraph beginning "This PR …" in imperative present, lists the Mathlib changes adapted to (file by file), has no section headings, and ends with `🤖 Prepared with __AGENT__`. This is a Lake-pin PR (no roadmap-target marker needed).

## Report
End with a concise summary: the mathlib rev and toolchain you bumped to, each file you changed and the breakage it fixed, the exact `lake build` / `lake exe axioms` result lines (proving green + axiom-clean), and the PR number/URL. Do not claim green unless you saw it.
