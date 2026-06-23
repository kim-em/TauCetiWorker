You are authoring a new pull request to FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a clean checkout of `main`. Pick the LOWEST-HANGING genuine roadmap target (or a clean prerequisite a target needs) and write the best small, complete, sorry-free PR you can — optimised to pass the project's review rubrics. Do honest mathematics. Work autonomously to completion.

## Choose a target
- **Work ONLY within the `__ONLY__` roadmap.** Read its plan under `__ROADMAP_DIR__/__ONLY__/` (provided read-only) — especially `README.md` and `Targets.lean` — and pick a target (or a clean prerequisite a target there needs) from THAT area only. Do not pick targets from other roadmap areas. (If `__ONLY__` is literally `any`, then any area under `__ROADMAP_DIR__/` is fair game, EXCEPT the skipped areas below.) Cite the exact target — e.g. `TauCetiRoadmap/__ONLY__/README.md` plus the specific item — in the PR body.
- **Never pick targets from these areas: `__SKIP__`** (they are being worked on by other contributors). If `__SKIP__` is `none`, there are no exclusions.
- **Within `__ONLY__`, do NOT work on these specific targets — other contributors have claimed them:**
  __CLAIMED__

  Everything else in `__ONLY__` is fair game. If the list above is `none`, there are no outstanding claims to avoid. These are cooperative claims registered by others; pick something genuinely distinct, not a near-variant of a claimed target.
- **Avoid duplicating open work.** List the PRs already in flight and read their titles and descriptions: `gh pr list --repo FormalFrontier/TauCeti --state open --limit 100 --json number,title,headRefName,body`. Also skim recently MERGED PRs (`--state merged`) so you build on, rather than repeat, what already landed. Do NOT pick a target an open or merged PR already covers or substantially overlaps (the same definition, the same roadmap item, or a near-identical API). Within `__ONLY__`, prefer the lowest-hanging target not yet taken; if every easy one is in flight, pick a genuine prerequisite none of them supply. When in doubt that your idea is distinct, choose something else.
- Read the review rubrics you'll be judged against under `__REVIEW_DIR__/rubrics/*.md` (provided read-only): scope, correctness, reuse, attribution, api-design, generality, placement, naming, documentation, proof-quality, deprecation.
- Before writing any declaration, `grep` the pinned Mathlib source to confirm it doesn't already exist (the `reuse` rubric is strict, and a generic fact transferred to a subtype is often already in Mathlib under a non-obvious import). The pinned Mathlib source is vendored in this checkout at `.lake/packages/mathlib` once `lake exe cache get` (or dependency resolution) has run — `grep` there; don't try to clone it from the network.

## Claim your target (so two agents don't author the same thing)
Once you have settled on a target, derive a short stable id for it and claim it BEFORE you start building. This lets other autonomous workers see the target is taken; it is cooperative, not a hard lock.
- **Target id:** `<slug>` = the target's most identifying phrase (its declaration name if it has one, else the key noun phrase of its statement/docstring), lowercased with every run of non-alphanumeric characters replaced by a single `-`. Keep it short and deterministic — another agent picking the *same* target should produce the *same* slug. Example: "the Galois group of a multiquadratic field is (ℤ/2)ⁿ" → `galois-group-multiquadratic-z2n`.
- **Claim it:**
  ```
  claim.sh acquire "author/__ONLY__/<slug>"
  ```
  Exit `0` = it's yours, proceed. Exit `1` = another agent already holds it — pick a DIFFERENT target and claim that instead. Exit `2` = the claim service hiccupped; proceed anyway (the duplicate sweeper is the backstop).
- **Record it in the PR body** (required — the PR will be rejected without it): include the exact line
  ```
  <!--tauceti-target:v1 {"focus":"__ONLY__","id":"<slug>"}-->
  ```
  using the SAME `<slug>` you claimed. This is what lets the worker recognize and close accidental duplicates of your target.

## Hard rules of the repo
- Code goes under `TauCeti/`. Just create your new module there; do NOT edit the root `TauCeti.lean`. The build globs every module under `TauCeti/`, so your file is compiled and axiom-audited without being listed, and the root aggregator is regenerated and committed automatically on `main` after merge — hand-edits to it only cause needless conflicts. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Everything under `namespace TauCeti`. Classic `import Mathlib...` syntax is simplest.
- Aim for ~150–400 lines of genuine, non-vacuous content. Smaller-but-green beats bigger-but-broken. No tautologies, no `True`-placeholder fields, no vacuous definitions. Follow Mathlib naming/docstring conventions; a `≃ₜ`-valued def is `...Homeomorph`, not `...Equiv`; never `@[simp]` a variable-head lemma; never silence a linter.
- Must build green AND pass the axiom audit (allowlist: `propext`, `Classical.choice`, `Quot.sound`; no `sorry`/`native_decide`/new axioms/`maxHeartbeats`).

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
If `lake build` is red, FIX IT or pick a smaller target. Never push red.

## Submit
- Create a branch `roadmap/<short-slug>` off `main`. Commit (message `feat: <subject>`; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push the new branch with the project's safe wrapper — and ONLY the wrapper:
  ```
  git-safe-push <branch>
  ```
  This create-only-pushes the branch (it fails closed if that branch name already exists, so two agents can't collide). Do NOT run a raw `git push`.
- Open the PR with the project's safe wrapper — and ONLY the wrapper:
  ```
  gh-safe-pr-create --repo FormalFrontier/TauCeti --base main --title "feat: <subject>" --body-file <file>
  ```
  Do NOT run a raw `gh pr create`. The PR body opens with a paragraph beginning "This PR …" in imperative present, cites the exact roadmap target, **includes the `<!--tauceti-target:v1 …-->` marker from the claim step** (the wrapper rejects the PR without it), names any Mathlib infrastructure you vendored (with attribution), has no section headings, and ends with `🤖 Prepared with __AGENT__`. Title `feat: <subject>`.

## Report
End with a concise summary: the target you chose and why it was lowest-hanging, the file(s) added and line count, the exact `lake build` / `lake exe axioms` result lines (proving green + axiom-clean), and the PR number/URL. Do not claim green unless you saw it.
