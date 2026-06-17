# Bubble end-to-end testing (Incus-equipped machine)

`tauceti` defaults every model-running mode to **bubble** (the sandbox); `--host` opts out. The host
paths are fully tested, but the bubble paths can only run on a machine with a working **Incus**
runtime, which the primary dev host does not have. This doc is the checklist for validating the bubble
paths on such a machine. Until it passes, keep `loop.sh`/`round.sh` as the production worker and do
**not** delete them (plan milestone M14 is gated on this).

Everything here mutates a real repo. Use scratch PRs you control, and a distinct `--worker-id` so the
claims/state don't collide with production workers.

## 0. Prerequisites

```bash
# Incus (NixOS): add to configuration.nix, then rebuild + init
#   virtualisation.incus.enable = true;
#   networking.nftables.enable = true;
#   users.users.<you>.extraGroups = [ "incus-admin" ];
sudo nixos-rebuild switch
sudo incus admin init --minimal

# bubble CLI
uv tool install git+https://github.com/kim-em/bubble.git
bubble list          # must succeed (probes the runtime), not "Incus is required"

# the worker checkout
cd TauCetiWorker
./tauceti doctor      # 'bubble' must show [ok]; gh auth ok; codex/claude creds present
```

`tauceti doctor` is the fast gate: it must report `bubble [ok]`. If it says MISSING, the CLI isn't on
PATH; if `bubble list` fails, the Incus runtime isn't up.

## 1. No-container sanity (cheap, run first)

These need no Incus and re-confirm the wiring on the new host:

```bash
./tauceti status                       # dashboard renders; quota line sane
python3 tests/parity_selectors.py      # 0 selector mismatches
bash tests/lifecycle.sh                # 5/5 (flock, fd-leak, timeout, signals)
python3 tests/agent_cmds.py            # host agent argv byte-for-byte
./tauceti work --dry-run               # picks one unit; prints sandbox=bubble (the opt-out default)

# Bubble command construction WITHOUT opening a container (prints argv, returns 0):
TAUCETI_WORKER_ID=echo TAUCETI_CLAIM_SH=/tmp/stub-claim.sh TAUCETI_AGENT_ECHO=1 \
  ./tauceti work --only roadmap --codex      # expect a `bubble open ... --command "env PATH=/opt/round:$PATH ... codex exec ..."` line
```

(`/tmp/stub-claim.sh` = a script that just `exit 0`s, so no real claim ref is written.)

## 2. Authoring/fixing in bubble (the existing run_in_bubble path)

For each workflow, run ONE real round against a scratch PR, with `--codex` and again with `--claude`.
Bubble is the default, so do **not** pass `--bubble` (it's a deprecated no-op) and do **not** pass
`--host`:

```bash
WID=bubble-test
# fix: needs a scratch PR of yours with a blocking review at head
TAUCETI_WORKER_ID=$WID ./tauceti work --only fix     --codex
# fix-ci: a scratch PR whose `build` check is red at head
TAUCETI_WORKER_ID=$WID ./tauceti work --only fix-ci  --codex
# rebase: a scratch PR made CONFLICTING vs main
TAUCETI_WORKER_ID=$WID ./tauceti work --only rebase  --codex
# bump: only fires if mathlib master is ahead of the pin and no bump PR is open
TAUCETI_WORKER_ID=$WID ./tauceti work --only bump     --codex
# roadmap: authors a new PR (stages TauCetiRoadmap/TauCetiReview as read-only mounts)
TAUCETI_WORKER_ID=$WID ./tauceti work --only roadmap  --claude
```

For each, verify:
- [ ] The container opens, the agent runs `lake exe cache get` / `lake build` **inside** it, and the
      round exits 0 (or a clean no-progress if nothing eligible).
- [ ] The push happened through bubble's auth proxy via `git-safe-push` (the host `kim-em` token never
      entered the container). Confirm the PR updated / opened.
- [ ] **The container is popped afterward** (`bubble list` shows no leftover `tauceti-worker-<wid>`).
      Kill a round mid-build (Ctrl-C / SIGTERM) and confirm cleanup still pops it.
- [ ] No host config leaked in: the agent had no `~/.claude/CLAUDE.md`, skills, or the other model's
      credential (only the one work-model credential was seeded).
- [ ] The shared Mathlib cache is an overlay (a round can't poison a later round's build).

## 3. Review in bubble (NEW path — review_in_bubble — highest risk)

This runs `uvx tauceti-review` **inside** bubble: the container boundary on the outside, the engine's
own read-only-tool + throwaway-HOME isolation on the inside (defense in depth). It is **untested** —
validate it carefully, the FORK case first.

```bash
WID=bubble-review
# (a) FIRST: a PR from a community FORK (the integration risk — proxy + base-repo PR API)
TAUCETI_WORKER_ID=$WID ./tauceti work --only review --codex      # picks a build-green, unreviewed PR
# (b) then a same-repo PR
```

Integration unknowns to confirm (these are the likely failure points):
- [ ] **`uvx` is available in the bubble image.** If not, the engine can't launch — the image needs
      `uv`. (Report this to kim-em/bubble if missing.)
- [ ] **A fork PR can be read through the repo-scoped proxy** (the engine fetches the PR diff/context
      via the base-repo PR API). If the proxy blocks fork branch access, review-in-bubble needs a proxy
      policy change — capture the exact failure.
- [ ] The reviewer model's credential is seeded (`--codex-credentials` / `--claude-credentials`); the
      engine authenticates inside its throwaway HOME.
- [ ] The scoreboard comment posts to the PR through the proxy (`gh api graphql`, repo-scoped — OK).
- [ ] The store is container-local/ephemeral (fine — the GitHub scoreboard is the source of truth).
- [ ] `--host` still falls back to host-side review (`uvx tauceti-review` on the host) and works.

If a fork PR can't be reviewed in bubble, that's the one finding that may need a bubble-side change;
note it and fall back to `--host` for review until resolved.

## 4. OpenRouter (DeepSeek / MiniMax) in bubble

Expected to **fail early** until kim-em/bubble#299 lands `pi` + openrouter.ai egress in the image:

```bash
./tauceti work --only roadmap --deepseek     # expect: "--agent deepseek requires --host until ... bubble#299"
./tauceti work --only roadmap --deepseek --host   # works today (host path)
```

Once the image has `pi`, set `TAUCETI_ALLOW_OPENROUTER_BUBBLE=1` and re-test the bubble path.

## 5. The loop, in bubble

```bash
# safest first: housekeeping-only loop (no model, no bubble)
TAUCETI_WORKER_ID=loop ./tauceti work --loop --only merge

# a real bubble loop (auto model, bubble default). Ctrl-C must stop the current round and exit.
TAUCETI_WORKER_ID=loop ./tauceti work --loop
```

Verify: the loop spawns each round as a child, a hung round is torn down at `ROUND_TIMEOUT` (the whole
process group, incl. the container), Ctrl-C reaches the round and pops the bubble, and the back-off
escalates on consecutive no-progress rounds.

## 6. Multi-worker (optional)

```bash
./tauceti work --loop --worker-id alice --isolate-home --only review
./tauceti work --loop --worker-id bob   --isolate-home --only roadmap
```

Verify two workers don't collide: distinct state/checkout/bubble per worker-id, claims dedup branch
work, and `git-safe-push`'s branch CAS rejects the loser of a concurrent push (it STOPs, doesn't
clobber).

## Sign-off

Bubble validation passes when sections 2, 3, and 5 are green for both `--codex` and `--claude`,
review-in-bubble works for a fork PR (or the gap is documented and `--host` review is the agreed
fallback), and no container leaks. Then M14 (delete `loop.sh`/`round.sh`, final README) can proceed.

## Results log (2026-06-16, macOS dev host + Colima/Incus)

This host *does* have a Colima/Incus runtime (contrary to the doc's premise), so validation was
attempted here. Status so far:

- **§1 no-container sanity — PASS.** `status` renders; `parity_selectors.py` 0 mismatches;
  `agent_cmds.py` 0 mismatches; `work --dry-run` prints `sandbox=bubble`; the `AGENT_ECHO` round emits
  the correct `bubble open … allowlist-write-graphql …` argv (repo-scoped, three RO mounts, only the
  work-model credential seeded, API keys cleared) and opens no container.
  - `lifecycle.sh` was non-portable: it shelled out to `setsid` (util-linux, absent on macOS), so test 3
    was a *false pass* and 4/5 failed. tauceti's actual signal handling is correct (SIGTERM→143,
    SIGINT→130, verified directly). Fixed the test to create the session via an inline `os.setsid()`
    launcher (+ a guard that can never group-kill the harness). **Now 5/5.**
- **Quota pacer / `--ignore-quota`.** Codex/Opus were only *paced* (over-pace), not exhausted. Renamed
  the old `--no-pace` flag to **`--ignore-quota`**; with an explicit `--codex`/`--claude` it runs the
  model regardless of pace (single-round and `--loop`).
- **§4 OpenRouter — doc is stale.** bubble#299 (pi + openrouter.ai egress) merged 2026-06-05, so the
  "expected to fail" note is outdated. The pre-flight `Die` still fires without
  `TAUCETI_ALLOW_OPENROUTER_BUBBLE=1` (confirmed). Real bubble OpenRouter rounds are *not yet validated*
  — blocked by the runtime bug below.
- **Bubble runtime bugs — found, filed, FIXED (bubble 0.7.21).** Three bugs blocked the private-
  `BUBBLE_HOME` path tauceti uses (none in tauceti itself):
  [#300](https://github.com/kim-em/bubble/issues/300) (`incus list <remote>:<name>` as one token →
  base-image rebuild failed), [#301](https://github.com/kim-em/bubble/issues/301) (cosmetic
  `Addr`/`Addrs` warning), [#304](https://github.com/kim-em/bubble/issues/304) (the host-singleton
  auth-proxy resolved its endpoint/token files against the caller's `BUBBLE_HOME`, not the daemon's, so
  a private home couldn't use the proxy). All fixed + merged. After upgrade, a private-`BUBBLE_HOME`
  `bubble open` works end-to-end: container launches, **auth proxy configured (repo-scoped, allowlisted
  GraphQL)**, repo cloned via shared objects, 9/9 Lake deps pre-populated, container popped clean.
- **In-container environment confirmed.** `pi` (0.73.1), `codex`, `claude`, `git`, `gh`, `lake`
  (`~/.elan/bin/lake`) all present; **OpenRouter egress works (HTTP 200)** and the allowlist blocks
  other hosts; `pi --provider openrouter` returns output (verified `PONG`). `uv`/`uvx` is **off by
  default** (`bubble tools` setting `no`) — Phase 3 review-in-bubble needs `bubble tools set uv yes`
  (a one-time image rebuild); not a bug.
  - **`jq` is missing from the bubble image** and is *not* a `bubble tools` option. `claim.sh` needs it,
    so in-container target-claiming fails (`jq: command not found`, exit 1). Cooperative/non-fatal for a
    single worker, but it breaks roadmap target de-dup and would bite multi-worker (Phase 7). Fix
    options: add `jq` to the image (request to kim-em/bubble), or make `claim.sh` jq-free (it could use
    the `python3` already in the image). TBD.

- **tauceti bugs found while validating the agent rounds (fixes staged, awaiting commit):**
  - **Roadmap prompt path.** The prompt sent the agent to `__ROADMAP_DIR__/<FOCUS>` = `/opt/roadmap/
    <FOCUS>`, but the roadmap repo nests areas under `TauCetiRoadmap/`, so the real path is
    `/opt/roadmap/TauCetiRoadmap/<FOCUS>`. Affected host **and** bubble. Fixed `ROADMAP_DIR` in both.
  - **Bubble codex used an unsupported model + the baked codex is stale.** The bubble seeds codex
    credentials but not host config (`--no-codex-config`), so in-container `codex exec` fell back to its
    built-in default (`gpt-5.3-codex`), which this ChatGPT-subscription account rejects. tauceti now
    pins `--model`. BUT validation then exposed a deeper, **bubble-image** problem: its codex CLI is too
    old — `gpt-5.5` (what the host uses successfully) returns "requires a newer version of Codex", and
    with the old CLI the account also rejects plain `gpt-5` and `gpt-5.3-codex`. So **codex-in-bubble is
    blocked until the bubble image ships a current codex** (report to kim-em/bubble). The `--model` pin
    is still correct for when the image is current. Pivoted Phase 3 to `--claude`/opus to validate the
    subscription authoring path meanwhile.
  - **False-success guard.** A model round that exits 0 but leaves no mark on GitHub (no push, new PR,
    or comment) is now surfaced as no-progress, not success — caught the original silent OpenRouter
    no-op. (`pi --print` is silent until the very end, so a round that bails mid-flight produces zero
    stdout; the guard checks the GitHub mutation, not stdout.)
  - Renamed `--no-pace` → **`--ignore-quota`**; fixed `lifecycle.sh` `setsid` portability (see §1).

- **HARD BLOCKER for all bubble authoring — shared-cache overlay is unwritable on macOS/Colima.**
  `bubble security set shared-cache overlay` mounts `/shared/mathlib-cache` as overlayfs
  (lowerdir on read-only virtiofs, `userxattr`). On macOS/Colima/virtiofs the container user **cannot
  write through the overlay** (`touch /shared/mathlib-cache/x` → Permission denied) even though the raw
  `upperdir` is writable — the classic overlayfs-over-virtiofs limitation. So `lake exe cache get` dies
  (`permission denied … /shared/mathlib-cache/curl.cfg`) and the container compiles **Mathlib from
  source**, which never finishes in a round. Confirmed by watching a `pi` round sit at 24 lean procs
  building `Mathlib/*` with no `Init.olean` after 15 min, never authoring. Filed
  [kim-em/bubble#306](https://github.com/kim-em/bubble/issues/306). **Until this is fixed (or shared-
  cache uses a writable per-bubble dir on macOS), no bubble authoring/fixing round can build in time.**
- **§3 OpenRouter (DeepSeek) authoring — agent path works up to the build.** With the roadmap path fixed,
  `pi` picks a real target and starts authoring (watched live via its session JSONL); it then blocks on
  the from-source Mathlib build (the #306 cache blocker). pi + OpenRouter themselves are fine.
- **Killing a round mid-build pops its container cleanly** (no leftover) — incidentally confirms the §2
  teardown checklist item.

### Bottom line (2026-06-16)
Bubble *infrastructure* works after #300/#301/#304. Bubble *authoring* is **not yet runnable end-to-end**
on this macOS/Colima host, blocked by:
1. **[#306](https://github.com/kim-em/bubble/issues/306)** shared-cache overlay unwritable → from-source
   Mathlib build (blocks pi/codex/claude alike). **The critical one.**
2. **Stale codex CLI in the image** → no account-supported model works (`gpt-5.5` needs newer CLI;
   `gpt-5`/`gpt-5.3-codex` rejected; bubble codex `0.114.0` vs host `0.136.0`). [kim-em/bubble#308](https://github.com/kim-em/bubble/issues/308).
3. **`jq` missing** → in-container `claim.sh` fails (roadmap dedup; matters for multi-worker). [kim-em/bubble#309](https://github.com/kim-em/bubble/issues/309).
4. **`uv` off by default** → `bubble tools set uv yes` before review-in-bubble (§3 review).
tauceti-side fixes (staged, uncommitted): roadmap path, codex `--model` pin, false-success guard,
`--ignore-quota`, `lifecycle.sh` portability. Resume Phase 3/4/5/6/7 once #306 (and the codex image) land.
- **§3 review-in-bubble — fork case is the documented gap.** No forks of TauCeti exist and only one
  GitHub account (kim-em) is available, so a community-fork PR can't be manufactured here; per the doc,
  treat fork review as the known gap and use `--host` review for forks. Same-repo review still testable
  (needs `uv` enabled).


### Bubble authoring VALIDATED end-to-end (2026-06-17, --codex)
After building `lean-v4.31.0` + refreshing the stale mirror, a real `--codex` fix round on PR #185
ran fully in-bubble: codex (gpt-5.5) read the blocking review, fixed it, `lake build` 3836 jobs green,
`lake exe axioms` clean, and `git-safe-push` pushed through the repo-scoped proxy (#185 head 62dd2cc8 ->
889e074). rc=0, container popped clean, no host token in container. A follow-up message-amend push was
correctly REJECTED by branch-CAS ("branch moved since checkout") and codex stopped — validating the
[HARD] push arbiter. So §2 (authoring/fixing in bubble) is GREEN for --codex.
- `--claude` §2: blocked only by Anthropic provider rate-limit (opus), not by tauceti/bubble — retry when quota recovers.
- Operational note: bubble's per-home git mirror must be refreshed and the project's exact `lean-vX.Y.Z`
  toolchain image must exist, else bubble selects a near-match toolchain image and elan's blocked
  download hangs the build. tauceti should `git fetch` the mirror + ensure the toolchain image before rounds.

### §2 `--claude` validated + false-success guard proven (2026-06-17)
A `--claude` fix round on PR #153 ran in-bubble (opus, no rate-limit this time): claude **contested**
the `scope` block by replying on the review thread through the proxy (visible as a kim-em review comment
at 03:42Z) — a legitimate non-code fix outcome. This exposed and fixed a gap in the false-success guard:
it only counted *issue* comments, so it wrongly flagged the contest as "no mark on GitHub". Now it counts
issue **and** review-thread comments (`pulls/{pr}/comments`). Net: §2 is green for both `--codex`
(code push, #185) and `--claude` (contest reply, #153); the guard correctly refuses to trust an agent's
self-reported success and keys off the actual GitHub mutation.

### Status summary for sign-off
- §1 sanity: GREEN. §2 authoring/fixing in bubble: GREEN (--codex push + --claude contest).
- §5 loop driver: GREEN (back-off escalation to cap; clean SIGINT exit; child-spawn; group-teardown).
- Branch-CAS [HARD] push arbiter: validated (stale 2nd push rejected on #185).
- §4 OpenRouter (pi) authoring: in progress (build env now works; round running).
- §3 review-in-bubble + §6 multi-worker: pending (need `uv` in the review image + a reviewer model).
- Bubble: all blockers fixed (#300/#301/#304/#306-307/#308/#309/#312). tauceti fixes committed
  (d962c41, eafd788).

### §3 review-in-bubble — blocked by engine fetch through the repo-scoped proxy (2026-06-17)
With `uv` enabled, review-in-bubble gets all the way to launching the engine: container opens, auth proxy
configured, PR checked out, `uvx` runs. It then fails because `uvx --from git+https://github.com/
FormalFrontier/TauCetiReview tauceti-review …` must clone **TauCetiReview** — a different repo from the
review target — and the proxy is repo-scoped to FormalFrontier/TauCeti, so the engine clone is rejected
(`Failed to resolve --with requirement: Git operation failed`). This is the §3 integration risk the
checklist anticipated. Fix is design-level (allow the engine repo through the proxy, mount/pre-stage the
engine, or bake `tauceti-review` into the bubble image). Until then, **`--host` review is the fallback**
(the engine runs on the host with its own clean-room isolation).
