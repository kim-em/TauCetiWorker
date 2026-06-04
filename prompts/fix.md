You are addressing AI code review on pull request #__PR__ of FormalFrontier/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a checkout of the repo, already on the PR's branch. Work autonomously to completion.

## Read the review
- The review is posted as a sticky scoreboard comment plus one thread per flagged rubric. Read them:
  - `gh pr view __PR__ --repo FormalFrontier/TauCeti --json comments`
  - `gh api "/repos/FormalFrontier/TauCeti/pulls/__PR__/comments?per_page=100"` (the per-rubric review threads; each root carries a `<!--tauceti-rubric:NAME-->` marker, and the finding text + suggested fix).
- The blocking rubrics are the ones marked ⛔ (block) or 🟡 (changes requested) on the scoreboard.

## Decide, per finding, on its merits
For each finding, judge whether it is actually correct:
- **If it is correct**, fix the code. Verify the fix empirically (does it build? does the claimed Mathlib lemma actually exist — `grep`/`#check`? does the suggested `@[simp]` lemma have a variable head, which the linter forbids?). Reviewers are sometimes confidently wrong; do not blindly comply.
- **If it is wrong**, do NOT comply. Reply on that rubric's thread explaining why, with evidence (a synth-check, a Mathlib citation, a build error). Post the reply to the thread root:
  `gh api -X POST "/repos/FormalFrontier/TauCeti/pulls/__PR__/comments/<ROOT_ID>/replies" -f body="..."`
  (A re-review reads these replies, so a well-evidenced contest can clear a wrong finding.)

## Rules of the repo (hard constraints)
- Code goes ONLY under `TauCeti/`. Do NOT touch `Scripts/`, `.github/`, `lakefile`/`lake-manifest.json`, or `TauCeti.lean`.
- Everything under `namespace TauCeti`.
- Must stay green AND axiom-clean: no `sorry`, no `native_decide`, no new axioms (allowlist: `propext`, `Classical.choice`, `Quot.sound`), no `maxHeartbeats` overrides, and **never silence a linter** (e.g. with `set_option ... false`) to force a change through — that is itself a reason to push back on the finding.

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
Iterate until green. Never push red.

## Submit
- Commit the fixes (message `<type>: <subject>`, imperative present; end the body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`).
- `git push` to the PR's branch (this updates the PR; a re-review runs separately).
- Do NOT open a new PR; do NOT touch other files.

## Report
End with a concise summary: which findings you fixed (and how you verified each), which you contested (and the evidence), and the exact `lake build` / `lake exe axioms` result lines proving green + axiom-clean. Do not claim green unless you saw it.
