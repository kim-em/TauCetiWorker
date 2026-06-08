You are fixing FAILING CI on pull request #__PR__ of FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. The `build` check is red. Work autonomously to completion: make CI green without weakening the PR.

## Find out what's actually failing
- See which checks failed and read their logs:
  - `gh pr checks __PR__ --repo FormalFrontier/TauCeti`
  - `gh run view <run-id> --repo FormalFrontier/TauCeti --log-failed` (use the run id from the failing check)
- Reproduce locally — this is the source of truth, not the log alone:
  ```
  lake exe cache get
  lake build
  lake exe axioms
  ```

## Fix it on its merits
- Diagnose the real cause (a broken proof, a renamed/missing Mathlib lemma, a linter error, an axiom-audit failure, a flaky/transient infra error). Fix the underlying problem.
- If the failure is genuinely transient/infra (e.g. cache fetch timeout) and the code builds clean locally, do NOT hack the code — push an empty commit to re-trigger CI (`git commit --allow-empty -m "chore: re-trigger CI"`) and say so in your report.
- Prefer the smallest correct fix. If a declaration is unsalvageable, it is better to remove it than to leave the PR red — but never gut the PR into vacuity; if almost nothing survives, stop and report that rather than pushing an empty shell.

## Rules of the repo (hard constraints)
- Code goes ONLY under `TauCeti/`. Do NOT touch `Scripts/`, `.github/`, `lakefile`/`lake-manifest.json`, or `TauCeti.lean`.
- Everything under `namespace TauCeti`.
- Must end green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force the build green — that defeats the point.

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red.

## Submit
- Commit the fix (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- `git push` to the PR's branch (this updates the PR; CI re-runs automatically).
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: what was failing, the root cause, what you changed (or that you only re-triggered transient CI), and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
