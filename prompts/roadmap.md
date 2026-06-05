You are authoring a new pull request to FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a clean checkout of `main`. Pick the LOWEST-HANGING genuine roadmap target (or a clean prerequisite a target needs) and write the best small, complete, sorry-free PR you can — optimised to pass the project's review rubrics. Do honest mathematics. Work autonomously to completion.

## Choose a target
- Read the roadmaps under `/opt/roadmap` (mounted read-only). Only do work that advances a specific roadmap target or supplies a prerequisite a specific target needs; cite the exact target in the PR body.
- **Avoid the area `__AVOID__`** — that is the area of the most recently opened PR, and the worker spreads across the roadmaps. Pick a target in a different area (a different roadmap, or a clearly different sub-piece).
- Read the review rubrics you'll be judged against under `/opt/review/rubrics/*.md` (mounted read-only): scope, correctness, reuse, attribution, api-design, generality, placement, naming, documentation, proof-quality, deprecation.
- Before writing any declaration, `grep` the pinned Mathlib source to confirm it doesn't already exist (the `reuse` rubric is strict, and a generic fact transferred to a subtype is often already in Mathlib under a non-obvious import). The pinned Mathlib source is vendored in this checkout at `.lake/packages/mathlib` once `lake exe cache get` (or dependency resolution) has run — `grep` there; don't try to clone it from the network.

## Hard rules of the repo
- Code goes ONLY under `TauCeti/`. Do NOT touch `Scripts/`, `.github/`, `lakefile`/`lake-manifest.json`, or `TauCeti.lean` (the lakefile globs `TauCeti.*`, so a new file is picked up automatically).
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
- `git push -u origin <branch>`, then `gh pr create` against `main`. The PR body opens with a paragraph beginning "This PR …" in imperative present, cites the exact roadmap target, names any Mathlib infrastructure you vendored (with attribution), has no section headings, and ends with `🤖 Prepared with __AGENT__`. Title `feat: <subject>`.

## Report
End with a concise summary: the target you chose and why it was lowest-hanging, the file(s) added and line count, the exact `lake build` / `lake exe axioms` result lines (proving green + axiom-clean), and the PR number/URL. Do not claim green unless you saw it.
