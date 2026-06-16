#!/usr/bin/env bash
# round.sh — perform exactly ONE unit of Tau Ceti work, then exit. Called by
# loop.sh once per round.
#
# Priority:
#   0. First, MERGE every PR the worker has already green-lit (all rubrics green
#      at head, TauCeti/-only, build green, cleanly mergeable), then ABANDON (close)
#      any PR that spent its lifetime review/fix budget without reaching green.
#      CI-side review and auto-merge are intentionally off, so the worker is the
#      only path that lands or retires PRs. These are cheap pre-passes (no quota),
#      then the round does ONE work unit:
#   1. Resolve conflicts: rebase one of kim-em's PRs that has become un-mergeable
#      (usually the root TauCeti.lean import line, after a sibling module PR merged
#      first) onto main — drive an agent. Bounded per-PR (MAX_REBASE_ATTEMPTS).
#   2. Review an open TauCeti PR whose current head has not been cleanly reviewed
#      yet (and whose CI build is green) — via the `tauceti-review` CLI. A moved
#      head (after a fix) is a new commit and gets reviewed; total review rounds
#      per PR are bounded by MAX_REVIEW_ROUNDS (then the abandon pre-pass closes it).
#   3. Fix one of kim-em's open PRs whose latest review (on the current head)
#      requests changes (🟡) or blocks (⛔) — drive an agent to address them.
#      Bounded per-head (MAX_FIX_ATTEMPTS) and per-PR over its lifetime
#      (MAX_FIX_PR_ATTEMPTS), so review↔fix can't ping-pong indefinitely.
#   4. Fix red CI on one of kim-em's open PRs whose "build" check has FAILED at
#      head (it can't be reviewed or review-fixed, so it would sit red forever).
#      Bounded per-head (MAX_CI_ATTEMPTS) and per-PR (MAX_CI_PR_ATTEMPTS).
#   5. Otherwise advance a roadmap target with a new PR, confined to the
#      ROADMAP_FOCUS area (default ReductiveGroups; empty ranges over all areas)
#      — unless the worker already has >= MAX_OPEN_PRS of its PRs open
#      (backpressure: drain before authoring more). The roadmap prompt also has
#      the agent check open PRs and avoid duplicating one already in flight.
#
# A GitHub API failure ABORTS the round (exit 1) rather than being read as "no
# work" — otherwise a transient outage would silently fall through to authoring.
#
# Inputs (env): CODEX_OK, OPUS_OK (1/0) — which subscription models have quota.
# Optional args:
#   --codex | --claude | --deepseek | --minimax  pin BOTH authoring/fixing and
#     reviewing to that one model. DeepSeek/MiniMax are OpenRouter models driven
#     by the `pi` agent and run ONLY when their flag is passed — no auto-dispatch
#     to a metered provider.
#   --bubble  run authoring/fixing inside a `bubble` container (kim-em/bubble),
#     repo-scoped to TauCeti, instead of on the host. Default: host.
#   --only <task>[,<task>...]  restrict the round to these work units (any of
#     merge, rebase, review, fix, fix-ci, bump, roadmap). Default (omitted): the
#     full cascade — "do whatever is most helpful". The quota-free housekeeping
#     pre-passes (merge ready PRs, abandon stuck ones, sweep duplicates) run in
#     every mode; 'merge' alone means "housekeeping only, no quota work".
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAUCETI="FormalFrontier/TauCeti"
ROADMAP="FormalFrontier/TauCetiRoadmap"
REVIEW="FormalFrontier/TauCetiReview"
ME="kim-em"
# Per-worker isolation: every local path is namespaced by a worker id so N copies on one host don't
# share state/checkout/store/bubble. loop.sh generates+persists a globally-unique id (hostname-uuid)
# and exports TAUCETI_WORKER_ID; a bare round.sh run falls back to "default". Sanitized for path/
# container-name use (lowercased, [a-z0-9-]).
WID="${TAUCETI_WORKER_ID:-default}"; WID="${WID,,}"; WID="${WID//[^a-z0-9-]/-}"
export TAUCETI_WORKER_ID="$WID"   # stable claim owner id for claim.sh / the push wrappers (host + heartbeat)
STORE_DIR="$HOME/.cache/tauceti-review/$WID/store/FormalFrontier__TauCeti"   # per-worker review store
STORE="$STORE_DIR/ledger.json"
CHECKOUT="$HERE/checkouts/$WID/TauCeti"   # host authoring checkout (used without --bubble)
STATE="$HERE/state/$WID"
MAX_FIX_ATTEMPTS=3        # per-head: stop fixing the same head after this many tries
MAX_FIX_PR_ATTEMPTS=7     # per-PR lifetime backstop: stop fixing a PR across all heads
MAX_REVIEW_ROUNDS=8       # lifetime review budget per PR: a non-green PR past this is abandoned (closed)
MAX_REVIEW_ERRORS=3       # give up re-trying a PR whose review keeps erroring (transient infra)
MAX_CI_ATTEMPTS=3         # per-head: stop trying to green a red CI head after this many tries
MAX_CI_PR_ATTEMPTS=5      # per-PR lifetime backstop for red-CI fixing across all heads
MAX_REBASE_ATTEMPTS=3     # per-PR: stop trying to rebase/resolve a conflicting PR after this many tries
MAX_BUMP_ATTEMPTS=3       # per mathlib-master-tip: stop trying to bump-to-and-fix a given tip after this many tries (a newer tip resets it)
MAX_OPEN_PRS=8            # backpressure: don't author new roadmap PRs while this many of the worker's PRs are already open
BUMP_BRANCH_PREFIX="bump-mathlib"   # worker's mathlib-bump branches; an open one (or a hopscotch/* PR) suppresses a new bump
ROADMAP_FOCUS="ReductiveGroups"   # confine authoring to this TauCetiRoadmap/ area; empty = spread across all areas
mkdir -p "$STATE" "$(dirname "$CHECKOUT")"

log() { echo "$(date '+%F %T') round: $*" >&2; }
die() { log "$*"; exit 1; }
# EX_NOPROGRESS: the round did NO productive work (queue backpressure, nothing reviewable, or a
# transient GitHub failure) — distinct from a real error (exit 1) and from success (exit 0). loop.sh
# reads this to back off with an escalating sleep instead of re-cycling every INTERROUND seconds and
# re-hammering the API (the failure mode that ran 700 no-op rounds against a rate-limited GitHub).
EX_NOPROGRESS=75
noprogress() { log "$*"; exit "$EX_NOPROGRESS"; }

# Args: an optional model override, an optional --bubble (sandbox) flag, and an
# optional --only task restriction.
FORCE=""; BUBBLE_MODE=0; ONLY=""
while (( $# )); do
    case "$1" in
        --codex|--claude|--deepseek|--minimax) FORCE="${1#--}";;
        --bubble) BUBBLE_MODE=1;;
        --only) shift; [[ -n "${1:-}" ]] || die "--only needs a task list"; ONLY="${ONLY:+$ONLY,}$1";;
        *) die "unknown argument: $1 (expected --codex|--claude|--deepseek|--minimax|--bubble|--only)";;
    esac
    shift
done

# Task restriction (--only). By default a round does "whatever is most helpful": it walks the full
# priority cascade and performs the first unit of work it finds. --only <task>[,<task>...] confines a
# round to the named work units — a worker pinned to one kind of work (e.g. a review-only or
# roadmap-only fleet member). The quota-free housekeeping pre-passes (merge ready PRs, abandon stuck
# ones, sweep duplicates) ALWAYS run, in every mode, so a focused worker still keeps the queue healthy.
# The pseudo-task 'merge' names "housekeeping only" — run the pre-passes, then do no quota-spending work.
ALLOWED_TASKS=" merge rebase review fix fix-ci bump roadmap "
ONLY="${ONLY// /}"   # tolerate "review, fix"
if [[ -n "$ONLY" ]]; then
    IFS=',' read -ra _only_tasks <<< "$ONLY"
    for _t in "${_only_tasks[@]}"; do
        [[ -z "$_t" ]] && continue
        [[ "$ALLOWED_TASKS" == *" $_t "* ]] \
            || die "unknown --only task '$_t' (valid: merge, rebase, review, fix, fix-ci, bump, roadmap)"
    done
fi
# want TASK — is this work-unit stage enabled this round? Empty ONLY ⇒ everything is enabled.
want() { [[ -z "$ONLY" ]] || [[ ",$ONLY," == *",$1,"* ]]; }

# OpenRouter models driven through the `pi` agentic loop (badlogic/pi-mono),
# billed per-token to OPENROUTER_API_KEY. Add a provider here and it is usable
# both as a --<name> override and as a review provider, with no other change.
# Chosen ids (env-overridable) are each provider's strongest agentic tool-using
# model on OpenRouter; pick the model with --<name> and override the id via the
# matching env var. (DeepSeek-Prover-V2 / ByteDance Seed-Prover are whole-proof
# search systems, not tool-using agents, so they cannot drive `pi` and are out.)
declare -A OPENROUTER_MODELS=(
    [deepseek]="${DEEPSEEK_MODEL:-deepseek/deepseek-v4-pro}"
    [minimax]="${MINIMAX_MODEL:-minimax/minimax-m3}"
)
declare -A AGENT_NAMES=(
    [codex]="Codex" [claude]="Claude Code" [deepseek]="DeepSeek" [minimax]="MiniMax"
)
PI_RUN="${PI_RUN:-$HOME/.claude/skills/pi/scripts/run.sh}"   # host runner (without --bubble)

# Models. Without an override, authoring/fixing prefers Codex (spare Opus) and
# review uses a single provider, Codex preferred (see REVIEWERS below). An
# override pins everything to the one named model.
CODEX_OK="${CODEX_OK:-0}"; OPUS_OK="${OPUS_OK:-0}"
if [[ -n "$FORCE" ]]; then
    WORK_MODEL="$FORCE"; AGENT="${AGENT_NAMES[$FORCE]}"; REVIEWERS="$FORCE"
    if [[ -n "${OPENROUTER_MODELS[$FORCE]:-}" ]]; then
        [[ -n "${OPENROUTER_API_KEY:-}" ]] || die "--$FORCE needs OPENROUTER_API_KEY in the environment"
        # `pi` on the host: the host path drives it directly, and bubble bakes it
        # into the container image when present (same pattern as claude/codex). The
        # PI_RUN wrapper is only used on the host path.
        command -v pi >/dev/null || die "--$FORCE needs the 'pi' agent on PATH (see ~/.claude/skills/pi/)"
        (( BUBBLE_MODE )) || [[ -x "$PI_RUN" ]] || die "--$FORCE (host) needs the pi runner at \$PI_RUN ($PI_RUN)"
    fi
else
    if (( CODEX_OK )); then WORK_MODEL=codex; AGENT="Codex"; else WORK_MODEL=claude; AGENT="Claude Code"; fi
    # Review with a SINGLE provider, Codex preferred (Codex is fine for every
    # rubric; the engine assigns rubrics to providers at random and only pins for
    # consistency, so there is no capability reason to keep Claude). Never pass
    # both: the per-rubric random split means one provider's outage silently
    # errors ~half the rubrics, and an errored integrity rubric blocks the PR —
    # which once benched the whole queue. Fall back to Opus only if Codex is dry.
    if   (( CODEX_OK )); then REVIEWERS="codex"
    elif (( OPUS_OK  )); then REVIEWERS="claude"
    else REVIEWERS=""; fi
fi

# One round at a time PER WORKER: rounds of the same worker id share its (now per-worker) checkout,
# state dir, and bubble, so they must not overlap. This is NOT cross-worker concurrency protection —
# different worker ids have different lock files and run in parallel by design (cross-worker
# de-contention is GitHub-side: branch --force-with-lease + claims). Auto-releases on exit.
exec 9>"$STATE/round.lock"
flock -n 9 || die "another round for worker '$WID' holds $STATE/round.lock — one round per worker at a time"

# Validate the ledger once; a malformed ledger would misclassify every PR.
if [[ -f "$STORE" ]] && ! jq empty "$STORE" 2>/dev/null; then
    die "review store ledger is malformed ($STORE) — aborting round"
fi

# A review round is "clean" if no rubric ended in the "error" state (a reviewer
# crash / parse failure). An errored round did NOT really review the PR — half its
# rubrics never ran — so the review gates below ignore it; otherwise a reviewer
# outage permanently benches a PR at its current head, looking "reviewed" when it
# wasn't. (The jq selects rounds with no "error" among their rubric states.)

# ===== Review state: read from GitHub, NOT a local ledger =====================
# In a multi-agent world the authoritative review state is the PR's canonical scoreboard comment
# (coordination contract §2), not this worker's private $STORE — another agent's review must be
# visible to us, or we re-review heads someone already did. The scoreboard carries a
# <!--tauceti-meta:v1 {...}--> JSON with .head_sha, .round (engine round number), and .runs[] (one
# per rubric, each .verdict ∈ approve|request_changes|block|error). We parse THAT. The helper names
# below are unchanged so the selection loops are untouched; only their source moved to GitHub.
# Scoreboard meta cache, with a SHORT cross-round TTL (not wiped at process start). Within a round
# every pass (merge/abandon/review/fix) reuses one fetch per PR; across rounds, a cached meta younger
# than SBCACHE_TTL is reused so a rapid re-cycle (or a flurry of workers) doesn't re-paginate every
# PR's comments every INTERROUND seconds. The TTL is short enough that genuinely new review state is
# picked up promptly, and bust_meta() invalidates a PR the moment THIS round changes it (review/fix/
# rebase), so merges aren't delayed. Crucially, a FAILED fetch falls back to the last cached value
# rather than '{}' — a transient GitHub outage must never read as "never reviewed" (which would
# re-review heads and, worse, let abandon/merge act on phantom state). [HARD for merge; COOP for dedup.]
SBCACHE="$STATE/cache/scoreboard"; mkdir -p "$SBCACHE"
SBCACHE_TTL="${TAUCETI_META_TTL:-120}"   # seconds a cached scoreboard meta stays fresh across rounds

# gh_meta PR — newest trusted scoreboard's meta JSON ("{}" if none). Trust = the
# <!--tauceti-scoreboard--> marker AND an author with repo association (OWNER/MEMBER/COLLABORATOR),
# so a random external comment can't forge review state.
gh_meta() {
    local pr="$1" cache="$SBCACHE/$1.json" meta age
    if [[ -f "$cache" ]]; then
        age=$(( $(date +%s) - $(stat -c %Y "$cache" 2>/dev/null || echo 0) ))
        (( age < SBCACHE_TTL )) && { cat "$cache"; return; }
    fi
    meta=$(gh api --paginate "/repos/$TAUCETI/issues/$pr/comments?per_page=100" \
            --jq '[.[] | select(.body|contains("<!--tauceti-scoreboard-->"))
                       | select(.author_association|IN("OWNER","MEMBER","COLLABORATOR"))]
                  | sort_by(.updated_at) | last | .body // ""' 2>/dev/null \
          | grep -oE '<!--tauceti-meta:v1 .*-->' | tail -1 \
          | sed -E 's/^<!--tauceti-meta:v1 //; s/-->$//')
    if [[ -z "$meta" ]]; then
        # Empty = transient fetch failure OR genuinely no scoreboard. Either way, if we have a prior
        # value, serve it (stale-but-real beats a phantom '{}'); only fall to '{}' with no cache at all.
        [[ -f "$cache" ]] && { cat "$cache"; return; }
        meta='{}'
    fi
    printf '%s\n' "$meta" > "$cache"; printf '%s\n' "$meta"
}
# bust_meta PR — drop a PR's cached meta so the NEXT gh_meta re-fetches. Call right after this round
# changes the PR's review state (a fresh review posted, or a new head pushed), so the next round's
# merge/select passes see the new scoreboard immediately instead of waiting out the TTL.
bust_meta() { rm -f "$SBCACHE/$1.json"; }

# ledger_head PR — head_sha of the latest scoreboard (used by the FIX step).
ledger_head() { gh_meta "$1" | jq -r '.head_sha // ""'; }
# ledger_clean_head PR — head_sha if the latest scoreboard round is CLEAN (every rubric ran without
# erroring), else "" — so a PR whose review errored stays eligible for a real re-review.
ledger_clean_head() {
    gh_meta "$1" | jq -r 'if ((.runs // []) | length > 0) and ((.runs // []) | all(.verdict != "error"))
                          then (.head_sha // "") else "" end'
}
# review_rounds PR — review-round count from the scoreboard's engine round number (GitHub-visible,
# shared across agents), minus the local resurrection baseline (state/round-base-PR) which gives a
# revived PR a fresh budget.
review_rounds() {
    local total base; total=$(gh_meta "$1" | jq -r '.round // 0'); base=$(counter "$STATE/round-base-$1")
    [[ "$total" =~ ^[0-9]+$ ]] || total=0
    local n=$(( total - base )); (( n < 0 )) && n=0; echo "$n"
}
# ledger_blocking PR HEAD — "1" if the latest scoreboard is at HEAD and any rubric is blocking
# (verdict not "approve" and not "error").
ledger_blocking() {
    gh_meta "$1" | jq -r --arg head "$2" '
        if (.head_sha // "") == $head
           and ((.runs // []) | map(select(.verdict != "approve" and .verdict != "error")) | length > 0)
        then 1 else 0 end'
}
# review_all_green PR HEAD — "1" if the latest scoreboard is at HEAD, ran, and every rubric approved.
review_all_green() {
    gh_meta "$1" | jq -r --arg head "$2" '
        if (.head_sha // "") == $head and ((.runs // []) | length > 0)
           and ((.runs // []) | all(.verdict == "approve"))
        then 1 else 0 end'
}
# sanitized non-negative integer from a state file (0 if absent/garbage).
counter() { local n; n=$(cat "$1" 2>/dev/null || echo 0); [[ "$n" =~ ^[0-9]+$ ]] || n=0; echo "$n"; }

# fill_prompt FILE KEY VAL [KEY VAL...] — substitute __KEY__ placeholders.
fill_prompt() {
    local f="$1"; shift; local out; out="$(cat "$f")"
    while (( $# )); do out="${out//__${1}__/$2}"; shift 2; done
    printf '%s' "$out"
}

# fetch_ref REPO DIR — keep DIR a shallow checkout of REPO's default branch on the
# host. Used for the roadmap/review reference repos: read in place on the host, or
# mounted read-only into a bubble (whose TauCeti-scoped proxy can't clone them).
fetch_ref() {
    local repo="$1" dir="$2"
    if [[ -d "$dir/.git" ]]; then
        # Worker-owned throwaway mirrors (no user work); reset hard and drop any
        # untracked/planted files so the staged copy is exactly upstream.
        git -C "$dir" fetch -q --depth 1 origin HEAD \
            && git -C "$dir" reset -q --hard FETCH_HEAD \
            && git -C "$dir" clean -fdxq
    else
        rm -rf "$dir"; mkdir -p "$(dirname "$dir")"
        git clone -q --depth 1 "https://github.com/$repo" "$dir"
    fi
}

# ===== Host authoring path (default) =========================================
# Runs the work model directly on the host against a reused checkout. Fast and
# simple, but NOT sandboxed — the agent has the host's full git/gh credentials
# and network. Use --bubble to sandbox instead.

# Make $CHECKOUT a clean checkout of TauCeti `main` (clone once; keep .lake for
# fast rebuilds, clean every other tracked/ignored leftover from a prior round).
prepare_checkout() {
    if [[ ! -d "$CHECKOUT/.git" ]]; then
        log "cloning $TAUCETI → $CHECKOUT (first run)"
        git clone -q "https://github.com/$TAUCETI" "$CHECKOUT" || return 1
    fi
    git -C "$CHECKOUT" fetch -q origin || return 1
    git -C "$CHECKOUT" switch -q main 2>/dev/null || git -C "$CHECKOUT" checkout -q -B main origin/main
    git -C "$CHECKOUT" reset -q --hard origin/main
    git -C "$CHECKOUT" clean -fdxq -e .lake
}

# run_agent CWD PROMPT — drive the work model on a coding task in CWD with full
# tool access on the host. Returns the agent's rc.
run_agent() {
    local cwd="$1" prompt="$2"
    export PATH="$HERE:$PATH"   # so the agent resolves git-safe-push / gh-safe-pr-create / claim.sh
    # 9>&- on each agent subshell: the agent (and any build daemon it spawns) must NOT inherit the
    # round.lock fd, or a lingering grandchild would keep the lock held past the round's end.
    if [[ "$WORK_MODEL" == codex ]]; then
        ( cd "$cwd" && codex exec --sandbox danger-full-access --skip-git-repo-check "$prompt" ) 9>&-
    elif [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        # OpenRouter model via the `pi` agentic loop: same prompt, full tools so it
        # can build, edit, and push like the others; billed per-token to
        # OPENROUTER_API_KEY (no subscription quota).
        ( cd "$cwd" && "$PI_RUN" openrouter "${OPENROUTER_MODELS[$WORK_MODEL]}" --prompt "$prompt" ) 9>&-
    else
        ( cd "$cwd" && env -u ANTHROPIC_API_KEY claude -p "$prompt" \
              --model opus --dangerously-skip-permissions ) 9>&-
    fi
}

# ===== Bubble authoring path (--bubble) ======================================
# Authoring/fixing runs inside a `bubble` container (kim-em/bubble): the per-round
# checkout, `lake exe cache get` / `lake build` / `lake exe axioms`, and every
# `git`/`gh` call happen IN the container, never on the host. GitHub access is
# mediated by bubble's auth proxy, repo-scoped to FormalFrontier/TauCeti — the
# host's `kim-em` token never enters the container, and a push outside TauCeti is
# rejected by the proxy (not merely flagged by CI after the fact). Only the one
# credential the work model needs is seeded; no host config (notably
# ~/.claude/CLAUDE.md) crosses the boundary.
BUBBLE="tauceti-worker-$WID"   # per-worker name so concurrent workers don't tear down each other's container
# A worker-private bubble data dir, so the sandbox can't inherit ambient
# `[[mounts]]` or a remote/cloud default from the operator's ~/.bubble/config.toml
# — the mount set and runtime are exactly what this script asks for. First use
# builds the worker's git mirrors + Mathlib cache here (slow once, then cached).
export BUBBLE_HOME="${TAUCETI_BUBBLE_HOME:-$HOME/.cache/tauceti-worker/$WID/bubble}"

# ensure_bubble_home — one-time hardening of the private bubble home: read-only
# shared Mathlib cache (per-round writable overlay) so a compromised round can't
# poison a later round's build. Best-effort; the round still runs if it fails.
ensure_bubble_home() {
    [[ -f "$BUBBLE_HOME/.worker-init" ]] && return 0
    mkdir -p "$BUBBLE_HOME"
    bubble security set shared-cache overlay >/dev/null 2>&1 || true
    touch "$BUBBLE_HOME/.worker-init"
}

# agent_cred_flags — bubble flags seeding ONLY the work model's credential, with
# all config and the other models' credentials kept out of the sandbox.
agent_cred_flags() {
    if [[ "$WORK_MODEL" == codex ]]; then
        printf '%s\n' --codex-credentials --no-codex-config --no-claude-credentials --no-claude-config
    elif [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        # pi/OpenRouter has no subscription credential to seed; its API key is
        # mounted via /opt/round (below). Keep BOTH subscriptions out of the sandbox.
        printf '%s\n' --no-claude-credentials --no-claude-config --no-codex-credentials --no-codex-config
    else
        printf '%s\n' --claude-credentials --no-claude-config --no-codex-credentials --no-codex-config
    fi
}

# agent_inner_cmd — the command bubble runs INSIDE the container. bubble execs it
# as `bash -lc 'cd <repo> && exec <cmd>'`, so it lands in the checkout with
# /etc/profile.d sourced (GH_TOKEN for the repo-scoped proxy). The prompt is read
# from a read-only mount, never threaded through bubble's argv parsing. Emptying
# the *_API_KEY vars forces subscription auth (mirrors the old host invocation).
agent_inner_cmd() {
    if [[ "$WORK_MODEL" == codex ]]; then
        printf '%s' 'env OPENAI_API_KEY= ANTHROPIC_API_KEY= codex exec --sandbox danger-full-access --skip-git-repo-check "$(cat /opt/round/prompt.txt)"'
    elif [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        # pi reads OPENROUTER_API_KEY (mounted read-only at /opt/round/openrouter.key);
        # the model id is fixed for this round. Needs the `pi` tool in the image and
        # openrouter.ai egress (the bubble `pi` tool provides both).
        printf 'env ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENROUTER_API_KEY="$(cat /opt/round/openrouter.key)" pi --provider openrouter --model %s --print "$(cat /opt/round/prompt.txt)"' \
            "${OPENROUTER_MODELS[$WORK_MODEL]}"
    else
        printf '%s' 'env ANTHROPIC_API_KEY= OPENAI_API_KEY= CLAUDECODE= claude -p "$(cat /opt/round/prompt.txt)" --dangerously-skip-permissions --model opus'
    fi
}

# run_in_bubble TARGET PROMPT [HOST:CONTAINER:ro ...] — open a fresh, repo-scoped
# bubble for TARGET (a bubble target like FormalFrontier/TauCeti or .../pull/N),
# run the work model on PROMPT to completion inside it, then pop it (--ephemeral).
# Trailing args are extra read-only host→container mounts (e.g. reference clones
# the TauCeti-scoped proxy would otherwise block the agent from fetching). Returns
# the agent's exit code.
run_in_bubble() {
    local target="$1" prompt="$2"; shift 2
    ensure_bubble_home
    local rounddir="$STATE/bubble-round"
    rm -rf "$rounddir"; mkdir -p "$rounddir"
    printf '%s' "$prompt" > "$rounddir/prompt.txt"
    # Stage the write wrappers (contract §1/§4) into the round dir, mounted read-only at /opt/round and
    # put on PATH inside the container, so the agent's ONLY push path is the branch-CAS git-safe-push.
    cp "$HERE/git-safe-push" "$HERE/gh-safe-pr-create" "$HERE/claim.sh" "$rounddir/" 2>/dev/null \
        && chmod +x "$rounddir/git-safe-push" "$rounddir/gh-safe-pr-create" "$rounddir/claim.sh"
    # OpenRouter models need their API key INSIDE the container — there is no proxy
    # for it (unlike GitHub). Stage it 0600 in the round dir; it mounts read-only at
    # /opt/round/openrouter.key and agent_inner_cmd exports it. Gone with the dir.
    if [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        ( umask 077; printf '%s' "${OPENROUTER_API_KEY:-}" > "$rounddir/openrouter.key" )
    fi

    # Clear any container left by a previous round that loop.sh's timeout SIGKILLed
    # before --ephemeral could fire (a SIGKILL can't be trapped). Rounds run one at
    # a time (enforced by the flock above), so the fixed name is self-cleaning.
    # The global `cleanup` EXIT trap pops the bubble if we're killed mid-run (BUBBLE_ACTIVE).
    bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true

    local mounts=( --mount "$rounddir:/opt/round:ro" ) m
    for m in "$@"; do mounts+=( --mount "$m" ); done
    local creds=(); while IFS= read -r m; do creds+=( "$m" ); done < <(agent_cred_flags)

    # Push-arbiter env crossing into the container: put /opt/round (the wrappers) on PATH and forward
    # the branch-CAS inputs the agent's git-safe-push / gh-safe-pr-create need. \$PATH stays single-ish
    # so it expands to the CONTAINER PATH inside bubble's `bash -lc`. We do NOT forward TAUCETI_CLAIM_*:
    # the [COOP] claim + heartbeat are managed host-side around this call (claim.sh in-container would
    # have to re-auth through the proxy); the branch CAS is the [HARD] guarantee and needs no claim.
    local tcenv="env PATH=/opt/round:\$PATH"
    [[ -n "${TAUCETI_PUSH_REF:-}"    ]] && tcenv+=" TAUCETI_PUSH_REF=$(printf '%q' "$TAUCETI_PUSH_REF")"
    [[ -n "${TAUCETI_PUSH_EXPECT:-}" ]] && tcenv+=" TAUCETI_PUSH_EXPECT=$(printf '%q' "$TAUCETI_PUSH_EXPECT")"
    [[ -n "${TAUCETI_PUSH_REMOTE:-}" ]] && tcenv+=" TAUCETI_PUSH_REMOTE=$(printf '%q' "$TAUCETI_PUSH_REMOTE")"
    [[ -n "${TAUCETI_TARGET_MARKER:-}" ]] && tcenv+=" TAUCETI_TARGET_MARKER=$(printf '%q' "$TAUCETI_TARGET_MARKER")"
    [[ -n "${TAUCETI_REQUIRE_TARGET_MARKER:-}" ]] && tcenv+=" TAUCETI_REQUIRE_TARGET_MARKER=$(printf '%q' "$TAUCETI_REQUIRE_TARGET_MARKER")"

    # --local forces the local Incus runtime (a host remote/cloud default would
    # reject the --mount). --github-security allowlist-write-graphql is the minimal
    # level that still lets the agent open a PR and post review-thread replies, all
    # repo-scoped to TauCeti by the proxy; pinning it keeps the worker independent
    # of the host bubble default (a `security.github=off` lockdown still wins and
    # would correctly abort).
    local rc=0
    BUBBLE_ACTIVE=1
    bubble open "$target" --shell --local --name "$BUBBLE" --ephemeral \
        --github-security allowlist-write-graphql \
        "${mounts[@]}" "${creds[@]}" --command "$tcenv $(agent_inner_cmd)" 9>&- || rc=$?

    # Don't rely on --ephemeral's pop alone: if it failed, the container (with the
    # mounted credential) would linger. Pop again before returning.
    bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true
    BUBBLE_ACTIVE=0
    return $rc
}

# ===== [COOP] claims + [HARD] push-arbiter wiring ============================
# Mutating tasks (rebase/fix/fix-ci) and authoring push through git-safe-push / gh-safe-pr-create
# (coordination contract §1/§4): the wrappers do a branch-level compare-and-swap so we never clobber
# another agent's work — cooperating or not. Around each mutating task the shell ALSO takes a [COOP]
# branch/<pr> claim and heartbeats it (dedup only; the CAS is the real guarantee). A single EXIT trap
# tears down the heartbeat, any open bubble, and any held claim, so nothing leaks when a do_* `exit`s.
CLAIM_SH="$HERE/claim.sh"
CLAIM_TTL_S="${CLAIM_TTL:-1500}"              # 25 min lease; expires if a dead worker stops heartbeating
CLAIM_HEARTBEAT_S="${CLAIM_HEARTBEAT:-300}"   # renew every 5 min while the agent runs
export CLAIM_TTL="$CLAIM_TTL_S"               # claim.sh reads this as its default ttl (renew uses it)
CLAIM_HELD=""        # branch/author claim key to release on exit ("" = none)
HEARTBEAT_PID=""     # background lease-renewer pid ("" = none)
BUBBLE_ACTIVE=0      # 1 while a bubble container is open, for the cleanup trap

stop_heartbeat() { [[ -n "$HEARTBEAT_PID" ]] && kill "$HEARTBEAT_PID" 2>/dev/null; HEARTBEAT_PID=""; }
start_heartbeat() {   # renew KEY every CLAIM_HEARTBEAT_S until killed, the lease is lost, OR round.sh dies
    local key="$1" rpid=$$   # $$ is round.sh's pid even inside the subshell below (not BASHPID)
    # 9>&- : do NOT inherit the round.lock fd — otherwise this subshell's `sleep` grandchild keeps the
    # lock held for up to CLAIM_HEARTBEAT_S after the round exits, blocking the next round ("holds round.lock").
    ( trap - EXIT INT TERM    # the renewer must never run the round's cleanup when it exits
      while sleep "$CLAIM_HEARTBEAT_S"; do
          kill -0 "$rpid" 2>/dev/null || exit 0     # round.sh gone (even via SIGKILL) → stop renewing so the lease can expire
          "$CLAIM_SH" renew "$key" >/dev/null 2>&1 || exit 0
      done ) 9>&- &
    HEARTBEAT_PID=$!
}

# cleanup — single EXIT handler: stop the heartbeat, pop any open bubble, release any held claim.
cleanup() {
    stop_heartbeat
    (( BUBBLE_ACTIVE )) && bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true
    [[ -n "$CLAIM_HELD" ]] && "$CLAIM_SH" release "$CLAIM_HELD" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 143' TERM   # loop.sh's timeout sends TERM first → run cleanup via the EXIT trap (release the lease)
trap 'exit 130' INT

# begin_branch_work PR HEAD REFNAME OWNER REPO — set the push-arbiter env for a mutating task and take
# the [COOP] branch claim. Returns 1 if another live worker already holds the claim (caller skips the
# PR — dedup); 0 to proceed. The push env is set ONLY on the proceed path, so a skipped candidate can't
# leak stale TAUCETI_PUSH_* into a later authoring round. A claim error (GitHub hiccup) is non-fatal:
# we proceed unclaimed, since git-safe-push's branch CAS is what actually protects the write.
begin_branch_work() {
    local pr="$1" head="$2" refname="$3" owner="$4" repo="$5" key="branch/$pr" rc
    "$CLAIM_SH" acquire "$key" "$CLAIM_TTL_S" >/dev/null 2>&1; rc=$?
    if (( rc == 1 )); then
        log "branch #$pr claimed by another worker — skipping (COOP dedup)"
        return 1
    fi
    export TAUCETI_PUSH_REF="$refname"
    export TAUCETI_PUSH_EXPECT="$head"
    export TAUCETI_PUSH_REMOTE="https://github.com/$owner/$repo"
    export TAUCETI_CLAIM_SH="$CLAIM_SH"
    if (( rc == 0 )); then
        CLAIM_HELD="$key"; export TAUCETI_CLAIM_KEY="$key"; start_heartbeat "$key"
    else
        log "claim acquire #$pr errored (rc=$rc) — proceeding unclaimed (branch CAS still protects)"
        unset TAUCETI_CLAIM_KEY 2>/dev/null || true
    fi
    return 0
}

# 1. Review --------------------------------------------------------------------
do_review() {
    local pr="$1" head="$2"
    [[ -n "$REVIEWERS" ]] || die "no reviewer models available"
    local errkey="$STATE/review-err-$pr" nrnd; nrnd=$(review_rounds "$pr")
    log "reviewing PR #$pr @ ${head:0:12} (review $((nrnd+1))/$MAX_REVIEW_ROUNDS budget, reviewers=$REVIEWERS)"
    # 9>&- : the reviewer (and its model subprocesses) must not inherit the round.lock fd.
    uvx --from "git+https://github.com/$REVIEW" tauceti-review "$pr" \
        --store "$STORE_DIR" --post --reviewer "$REVIEWERS" --expect-head "$head" 9>&-
    local rc=$?
    # A clean run records a ledger round (the real-review cap counts those); reset
    # the transient-error counter. A failure didn't review, so bound the retries.
    if (( rc == 0 )); then echo 0 > "$errkey"; bust_meta "$pr"   # fresh scoreboard posted — re-read next round
    else local e; e=$(counter "$errkey"); echo $((e+1)) > "$errkey"; fi
    return $rc
}

# 2. Fix -----------------------------------------------------------------------
do_fix() {
    local pr="$1" head="$2"
    local hkey="$STATE/fix-$pr-${head:0:12}" pkey="$STATE/fix-pr-$pr" n np rc
    n=$(counter "$hkey"); np=$(counter "$pkey")
    # Count the attempt UP FRONT. The host path can fail (and `return`) at
    # checkout below; counting first stops an un-checkout-able PR from being
    # reselected every round forever and starving review/roadmap work.
    echo $((n+1))  > "$hkey"
    echo $((np+1)) > "$pkey"
    if (( BUBBLE_MODE )); then
        log "fixing PR #$pr (head $((n+1))/$MAX_FIX_ATTEMPTS, PR total $((np+1))/$MAX_FIX_PR_ATTEMPTS) with $AGENT in a bubble"
        # bubble checks out the PR branch inside the container; the agent reads the
        # review, fixes, builds, and pushes to the PR branch — all repo-scoped.
        run_in_bubble "$TAUCETI/pull/$pr" \
            "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    else
        prepare_checkout || { log "checkout failed for #$pr — skipping this attempt"; return 1; }
        # --force resets a diverged/stale local PR branch (left by a prior round
        # whose remote was since rewritten) to the PR's remote head, instead of
        # aborting on a non-fast-forward and failing every attempt.
        ( cd "$CHECKOUT" && gh pr checkout "$pr" --force ) || { log "gh pr checkout #$pr failed — skipping this attempt"; return 1; }
        # CAS against the head we actually checked out (a concurrent push since round-start would make
        # the round-start oid stale and fail every push); falls back to the round-start head.
        export TAUCETI_PUSH_EXPECT="$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || echo "$head")"
        log "fixing PR #$pr (head $((n+1))/$MAX_FIX_ATTEMPTS, PR total $((np+1))/$MAX_FIX_PR_ATTEMPTS) with $AGENT on the host"
        run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    fi
    (( rc == 0 )) && bust_meta "$pr"   # head likely moved — re-read the scoreboard next round
    return $rc
}

# 3. Fix red CI ----------------------------------------------------------------
# Same shape as do_fix (bubble or host), but drives prompts/fix-ci.md to green a
# PR whose "build" check has failed. Counters are kept separate from do_fix so
# the two kinds of fix don't share a budget.
do_fix_ci() {
    local pr="$1" head="$2"
    local hkey="$STATE/ci-$pr-${head:0:12}" pkey="$STATE/ci-pr-$pr" n np rc
    n=$(counter "$hkey"); np=$(counter "$pkey")
    echo $((n+1))  > "$hkey"
    echo $((np+1)) > "$pkey"
    if (( BUBBLE_MODE )); then
        log "fixing red CI on PR #$pr (head $((n+1))/$MAX_CI_ATTEMPTS, PR total $((np+1))/$MAX_CI_PR_ATTEMPTS) with $AGENT in a bubble"
        run_in_bubble "$TAUCETI/pull/$pr" \
            "$(fill_prompt "$HERE/prompts/fix-ci.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    else
        prepare_checkout || { log "checkout failed for #$pr — skipping this attempt"; return 1; }
        # --force resets a diverged/stale local PR branch (left by a prior round
        # whose remote was since rewritten) to the PR's remote head, instead of
        # aborting on a non-fast-forward and failing every attempt.
        ( cd "$CHECKOUT" && gh pr checkout "$pr" --force ) || { log "gh pr checkout #$pr failed — skipping this attempt"; return 1; }
        export TAUCETI_PUSH_EXPECT="$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || echo "$head")"
        log "fixing red CI on PR #$pr (head $((n+1))/$MAX_CI_ATTEMPTS, PR total $((np+1))/$MAX_CI_PR_ATTEMPTS) with $AGENT on the host"
        run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/fix-ci.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    fi
    (( rc == 0 )) && bust_meta "$pr"   # head likely moved — re-read the scoreboard next round
    return $rc
}

# 4. Resolve conflicts ---------------------------------------------------------
# Drive an agent to rebase a PR that has become un-mergeable (mergeable=CONFLICTING)
# onto current main and resolve the conflicts. Most conflicts are on the root
# TauCeti.lean import list (several module PRs each add a line; whoever merges
# first conflicts the rest), which is mechanical to union — but rebasing is an
# agent's job (it must rebuild green and may face real content conflicts), not a
# scripted git merge. Bounded per-PR so a PR that can't be cleanly rebased is
# eventually abandoned by the budget rule instead of looping.
do_rebase() {
    local pr="$1" head="$2"
    local pkey="$STATE/rebase-pr-$pr" np rc; np=$(counter "$pkey")
    echo $((np+1)) > "$pkey"   # count up front: a failed checkout can't wedge the loop
    if (( BUBBLE_MODE )); then
        log "resolving conflicts on PR #$pr (attempt $((np+1))/$MAX_REBASE_ATTEMPTS) with $AGENT in a bubble"
        run_in_bubble "$TAUCETI/pull/$pr" \
            "$(fill_prompt "$HERE/prompts/rebase.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    else
        prepare_checkout || { log "checkout failed for #$pr — skipping this attempt"; return 1; }
        ( cd "$CHECKOUT" && gh pr checkout "$pr" --force ) || { log "gh pr checkout #$pr failed — skipping this attempt"; return 1; }
        # CAS the force-push against the pre-rebase head we checked out (lease succeeds iff no one else
        # pushed since): a rewritten history is allowed, a concurrent push is not.
        export TAUCETI_PUSH_EXPECT="$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || echo "$head")"
        log "resolving conflicts on PR #$pr (attempt $((np+1))/$MAX_REBASE_ATTEMPTS) with $AGENT on the host"
        run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/rebase.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    fi
    (( rc == 0 )) && bust_meta "$pr"   # head moved (rebased) — re-read the scoreboard next round
    return $rc
}

# 4. Roadmap -------------------------------------------------------------------
# Both modes stage the read-only roadmap/review reference repos on the host (the
# in-bubble proxy is TauCeti-scoped, so the agent can't fetch them itself) and
# tell the agent where to read them via __ROADMAP_DIR__ / __REVIEW_DIR__.
# FOCUS is the single TauCetiRoadmap/ area to confine authoring to (e.g.
# ReductiveGroups), or "any" to range over all areas. It reaches the prompt as
# __FOCUS__.
do_roadmap() {
    local focus="$1" refs="$STATE/refs"
    fetch_ref "$ROADMAP" "$refs/roadmap" || die "fetch $ROADMAP failed"
    fetch_ref "$REVIEW"  "$refs/review"  || die "fetch $REVIEW failed"
    # Authoring path of the [HARD] write arbiter (contract §4): the agent claims its target
    # (author/<focus>/<id>) via claim.sh, stamps the body with <!--tauceti-target--> for the dup
    # sweeper, create-only-pushes the new branch with git-safe-push, and opens the PR with
    # gh-safe-pr-create. Requiring the target marker makes gh-safe-pr-create reject a PR the sweeper
    # could not later de-duplicate. (claim.sh + the wrappers are on PATH: host via run_agent, bubble via
    # the staged /opt/round mount.) The agent owns target SELECTION — Targets.lean has no stable machine
    # id to enumerate from the shell — so the claim is acquired agent-side; correctness still rests on
    # the [HARD] sweeper + branch CAS, with the claim as [COOP] dedup.
    export TAUCETI_REQUIRE_TARGET_MARKER=1
    if (( BUBBLE_MODE )); then
        log "roadmap round with $AGENT in a bubble (focus: $focus)"
        run_in_bubble "$TAUCETI" \
            "$(fill_prompt "$HERE/prompts/roadmap.md" FOCUS "$focus" AGENT "$AGENT" \
                ROADMAP_DIR /opt/roadmap REVIEW_DIR /opt/review)" \
            "$refs/roadmap:/opt/roadmap:ro" "$refs/review:/opt/review:ro"
    else
        prepare_checkout || die "checkout failed"
        log "roadmap round with $AGENT on the host (focus: $focus)"
        run_agent "$CHECKOUT" \
            "$(fill_prompt "$HERE/prompts/roadmap.md" FOCUS "$focus" AGENT "$AGENT" \
                ROADMAP_DIR "$refs/roadmap" REVIEW_DIR "$refs/review")"
    fi
}

# 4.5 Bump --------------------------------------------------------------------
# When mathlib's master has advanced past our pin, drive an agent to bump to the
# current master tip and FIX whatever breaks in TauCeti/ — the automated form of a
# hand bump: set lean-toolchain to match mathlib, `lake update mathlib`, build, fix
# the breakage, stay axiom-clean. The agent touches ONLY TauCeti/ + lean-toolchain +
# lake-manifest.json (never the lakefile); CI's bump-guard re-validates the pins as a
# forward move, and a green bump can auto-merge after review. Bounded per master-tip so
# a genuinely hard bump can't churn — a newer tip resets the budget. hopscotch (the
# downstream-reports automation) opens its own last-known-good bump PRs; an open one of
# those, or any open bump branch of ours, suppresses this so the two never collide.
mathlib_master_tip() {
    gh api repos/leanprover-community/mathlib4/commits/master --jq '.sha' 2>/dev/null
}
our_mathlib_pin() {
    gh api "repos/${TAUCETI}/contents/lake-manifest.json?ref=main" --jq '.content' 2>/dev/null \
        | base64 -d 2>/dev/null \
        | python3 -c 'import json,sys
m=json.load(sys.stdin); print(next((p["rev"] for p in m["packages"] if p.get("name")=="mathlib"), ""))' 2>/dev/null
}
# bump_candidate <open-json> — echo the target master sha and return 0 if a bump is
# warranted (master ahead of our pin, no bump/hopscotch PR open, under budget); else 1.
bump_candidate() {
    local open="$1"
    if echo "$open" | jq -e --arg p "$BUMP_BRANCH_PREFIX" '.[]
        | select(.isDraft|not)
        | select((.headRefName|startswith($p)) or (.headRefName|startswith("hopscotch/")))' >/dev/null 2>&1; then
        return 1   # a bump PR (ours or hopscotch's) already owns the lane
    fi
    local tip pin; tip=$(mathlib_master_tip); pin=$(our_mathlib_pin)
    [[ -n "$tip" && -n "$pin" ]] || return 1   # API hiccup → don't bump on bad data
    [[ "$tip" == "$pin" ]] && return 1         # already at master tip
    (( $(counter "$STATE/bump-${tip:0:12}") >= MAX_BUMP_ATTEMPTS )) && return 1
    echo "$tip"
}

do_bump() {
    local tip="$1" pkey="$STATE/bump-${1:0:12}" np branch
    np=$(counter "$pkey"); echo $((np+1)) > "$pkey"   # count up front: a wedged attempt can't loop
    branch="${BUMP_BRANCH_PREFIX}-${tip:0:12}"        # deterministic: concurrent workers collide here, the create-only CAS picks one
    if (( BUBBLE_MODE )); then
        log "bump to mathlib master ${tip:0:12} (attempt $((np+1))/$MAX_BUMP_ATTEMPTS) with $AGENT in a bubble"
        run_in_bubble "$TAUCETI" \
            "$(fill_prompt "$HERE/prompts/bump.md" TARGET "$tip" BRANCH "$branch" AGENT "$AGENT")"
    else
        prepare_checkout || die "checkout failed"
        log "bump to mathlib master ${tip:0:12} (attempt $((np+1))/$MAX_BUMP_ATTEMPTS) with $AGENT on the host"
        run_agent "$CHECKOUT" \
            "$(fill_prompt "$HERE/prompts/bump.md" TARGET "$tip" BRANCH "$branch" AGENT "$AGENT")"
    fi
}

# 0. Merge ---------------------------------------------------------------------
# Drain every PR the worker has already green-lit. Mirrors the engine's own gate
# (TauCetiReview review.py --auto-merge): merge iff every rubric is green on the
# CURRENT head AND the PR changes only TauCeti/ files, plus the build is green and
# Git reports it cleanly mergeable. Runs as kim-em (admin; enforce_admins is off,
# so this satisfies branch protection just as the review bot's approval would).
# CI-side review/merge is intentionally disabled, so this is the ONLY path that
# lands PRs. No quota/agent, so it runs every round; each merge is logged and a
# failed merge is left open for human attention.
merge_ready_prs() {
    local list; list=$(gh pr list --repo "$TAUCETI" --state open --author "$ME" \
        --json number,headRefOid,isDraft,mergeable,statusCheckRollup,files 2>/dev/null) \
        || { log "merge: gh pr list failed — skipping merge pass"; return 0; }
    local pr head
    while IFS=$'\t' read -r pr head; do
        [[ -z "$pr" ]] && continue
        # GitHub scoreboard (contract §2/§5): latest review must be at this exact head, every rubric
        # approved. This is the merge gate; GitHub itself serializes the merge ([HARD]).
        [[ "$(review_all_green "$pr" "$head")" == "1" ]] || continue
        log "merging PR #$pr (all rubrics green @ ${head:0:12}, TauCeti/ + root only)"
        # --admin: kim-em is a repo admin and enforce_admins is off, so this
        # overrides the "1 approving review" branch policy (CI's bot approval is
        # disabled). Without it gh refuses: "base branch policy prohibits the merge".
        if gh pr merge "$pr" --repo "$TAUCETI" --squash --delete-branch --admin; then
            log "merged PR #$pr"
        else
            log "merge of PR #$pr failed — left open for human attention"
        fi
    done < <(echo "$list" | jq -r '.[]
        | select(.isDraft | not)
        | select(.mergeable == "MERGEABLE")
        | select([.statusCheckRollup[]? | select(.name=="build")] | any(.conclusion=="SUCCESS"))
        | select([.files[].path] | (length > 0 and all(startswith("TauCeti/") or . == "TauCeti.lean")))
        | [(.number|tostring), .headRefOid] | @tsv')
}

# has_human_activity PR — "1" if anyone other than this worker's automated identities has engaged:
# a protective label (keep/hold/wip/human), or a comment/review by a login that isn't us or a bot.
# Conservative and FAIL-SAFE: on any API error or doubt it returns 1 (treat as human-touched), so we
# never auto-close a PR a person cares about. (Caveat: the worker posts as $ME, so a HUMAN acting as
# $ME is indistinguishable from the worker — a person protecting a PR should use the 'keep' label.)
has_human_activity() {
    local pr="$1" out
    out=$(gh pr view "$pr" --repo "$TAUCETI" --json labels,comments,reviews 2>/dev/null) || return 0
    jq -e '(.labels//[]) | any(.name | ascii_downcase | IN("keep","hold","wip","human","do-not-close"))' \
        <<<"$out" >/dev/null && return 0
    jq -e --arg me "$ME" '
        [ (.comments//[]),(.reviews//[]) | .[] | .author.login // "" ]
        | map(select(. != "" and . != $me and (endswith("[bot]")|not) and (startswith("app/")|not)))
        | length > 0' <<<"$out" >/dev/null && return 0
    return 1
}

# 0b. Abandon -----------------------------------------------------------------
# Close our PRs that can no longer make progress. SAFE FOR MANY AGENTS (contract §5): we close only a
# PR that is (a) ours (--author $ME), (b) NOT green at head, (c) whose CURRENT head is the one that was
# actually reviewed (so a freshly-pushed-but-unreviewed fix is never closed), (d) past the GitHub-
# visible review-round budget (the scoreboard's round number, shared across agents — never a private
# local fix counter), and (e) free of human activity. The branch is kept (no --delete-branch) so the
# work can be revived. Cheap, no quota — runs every round.
abandon_stuck_prs() {
    local list; list=$(gh pr list --repo "$TAUCETI" --state open --author "$ME" \
        --json number,headRefOid,isDraft 2>/dev/null) \
        || { log "abandon: gh pr list failed — skipping"; return 0; }
    local pr head total
    while IFS=$'\t' read -r pr head; do
        [[ -z "$pr" ]] && continue
        [[ "$(review_all_green "$pr" "$head")" == "1" ]] && continue       # green merges elsewhere
        [[ "$(ledger_head "$pr")" == "$head" ]] || continue                # head not yet reviewed → let it
        total=$(review_rounds "$pr")
        (( total >= MAX_REVIEW_ROUNDS )) || continue                       # GitHub-visible budget
        if has_human_activity "$pr"; then
            log "abandon: PR #$pr past budget but has human activity / keep-label — leaving for a human"
            continue
        fi
        log "abandoning PR #$pr (review rounds=$total) — review budget spent at the reviewed head without reaching green"
        if gh pr close "$pr" --repo "$TAUCETI" \
            --comment "Closing automatically: this PR used its full review budget (${total} review rounds) without reaching an all-green review at its current head, so an autonomous worker is abandoning it to keep the queue moving. The branch is left in place — revive and finish it by hand if it's worth completing, or add the \`keep\` label to stop auto-close." 2>&1 | tail -1; then
            log "abandoned PR #$pr"
        else
            log "abandon of PR #$pr failed"
        fi
    done < <(echo "$list" | jq -r '.[] | select(.isDraft|not) | [(.number|tostring), .headRefOid] | @tsv')
}

# 0c. De-duplicate authored PRs ------------------------------------------------
# Close a NEWER PR that authors the SAME roadmap target as an older open one. SAFE FOR MANY AGENTS
# (contract §4/§5): the target id comes from the machine-readable <!--tauceti-target:v1 {focus,id}-->
# marker the authoring agent stamps; we act ONLY when two of OUR open PRs carry the EXACT same id,
# keep the lowest PR number, and close the higher one(s) — never one with human activity, never a PR
# without a parseable marker. A non-cooperator's duplicate (no marker, or a different id) is left alone;
# the branch-CAS already stops any write clash, so the worst it causes is bounded duplicate review.
# Cheap, no quota. [HARD guard on the close; COOP on the dedup itself.]
sweep_duplicate_authored_prs() {
    local list; list=$(gh pr list --repo "$TAUCETI" --state open --author "$ME" \
        --json number,body,isDraft 2>/dev/null) \
        || { log "dup-sweep: gh pr list failed — skipping"; return 0; }
    declare -A seen   # target-id -> lowest (oldest) open PR number that owns it
    local pr body id
    while IFS=$'\t' read -r pr body; do
        [[ -z "$pr" ]] && continue
        id=$(printf '%s' "$body" | grep -oE '<!--tauceti-target:v1 \{[^}]*\}-->' | head -1 \
             | sed -nE 's/.*"id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p')
        [[ -z "$id" ]] && continue   # no parseable target marker → never treated as a duplicate
        if [[ -n "${seen[$id]:-}" ]]; then
            if has_human_activity "$pr"; then
                log "dup-sweep: PR #$pr duplicates #${seen[$id]} (target '$id') but has human activity — leaving"
                continue
            fi
            log "dup-sweep: closing PR #$pr — duplicate authored target '$id' (older open #${seen[$id]} kept)"
            if gh pr close "$pr" --repo "$TAUCETI" \
                --comment "Closing automatically: this PR authors the same roadmap target (\`$id\`) as the older open #${seen[$id]}. An autonomous worker keeps the earlier PR and closes this duplicate to avoid redundant review. The branch is left in place — add the \`keep\` label if it is intentionally distinct." >/dev/null 2>&1; then
                log "dup-sweep: closed #$pr"
            else
                log "dup-sweep: close of #$pr failed"
            fi
        else
            seen[$id]="$pr"
        fi
    done < <(echo "$list" | jq -r '.[] | select(.isDraft|not)
        | [(.number|tostring), (.body // "" | gsub("[\n\r\t]";" "))] | @tsv' | sort -n)
}

# ------------------------------------------------------------------------------
main() {
    # Land anything already green-lit, then abandon anything that has exhausted its
    # budget without converging — both before doing more work (CI merge/review is
    # off; the worker is the only path that merges or retires PRs). Cheap, no quota.
    merge_ready_prs
    sweep_duplicate_authored_prs
    abandon_stuck_prs

    # One authoritative fetch of open PRs; a GitHub failure aborts the round.
    local open; open=$(gh pr list --repo "$TAUCETI" --state open \
        --json number,headRefOid,headRefName,headRepositoryOwner,headRepository,isDraft,statusCheckRollup,author,mergeable) \
        || noprogress "gh pr list failed (GitHub API?) — aborting round, not falling through to authoring"

    # Visibility: how much review work the queue actually offers. If 'reviewable'
    # is 0 while PRs are open, review silently can't fire (build pending/renamed,
    # or every candidate is past its re-review cap) and the round falls through to
    # authoring — this log makes that degradation visible instead of silent.
    local n_open n_reviewable
    n_open=$(echo "$open" | jq '[.[] | select(.isDraft|not)] | length')
    n_reviewable=$(echo "$open" | jq '[.[] | select(.isDraft|not)
        | select([.statusCheckRollup[]? | select(.name=="build")] | any(.conclusion=="SUCCESS"))] | length')
    log "open PRs: ${n_open} non-draft, ${n_reviewable} build-green (before re-review caps)"

    # 1) Resolve conflicts: kim-em's PRs that have become un-mergeable (typically
    #    the root TauCeti.lean import line, after a sibling module PR merged first).
    #    Do this before review — rebasing produces a new head, so reviewing the old
    #    one would be wasted. Bounded per-PR; a PR that can't be rebased rides to
    #    the review budget and is abandoned. Skip past-budget PRs (abandon owns them).
    local pr head refname owner repo
    if want rebase; then
    while read -r pr head refname owner repo; do
        [[ -z "$pr" ]] && break
        (( $(review_rounds "$pr") >= MAX_REVIEW_ROUNDS )) && continue
        (( $(counter "$STATE/rebase-pr-$pr") >= MAX_REBASE_ATTEMPTS )) && continue
        begin_branch_work "$pr" "$head" "$refname" "$owner" "$repo" || continue   # [COOP] dedup: another worker has it
        do_rebase "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r --arg me "$ME" '.[]
        | select(.isDraft|not) | select(.author.login==$me)
        | select(.mergeable=="CONFLICTING")
        | "\(.number) \(.headRefOid) \(.headRefName) \(.headRepositoryOwner.login) \(.headRepository.name)"')
    fi

    # 2) Review: first non-draft, build-green PR whose CURRENT head has not been
    #    cleanly reviewed yet. The head-guard (clean_head != head) is what prevents
    #    re-reviewing the same commit; a head that moved after a fix is a NEW commit
    #    and gets reviewed even if earlier heads were reviewed — otherwise a fix
    #    leaves the PR stuck (reviewed at an old head, unreviewable at the new one).
    #    The lifetime budget (MAX_REVIEW_ROUNDS) bounds total churn; a PR that blows
    #    through it without going green is abandoned by the abandon pre-pass.
    if want review; then
    while read -r pr head; do
        [[ -z "$pr" ]] && break
        [[ "$(ledger_clean_head "$pr")" == "$head" ]] && continue
        (( $(review_rounds "$pr") >= MAX_REVIEW_ROUNDS )) && continue
        (( $(counter "$STATE/review-err-$pr") >= MAX_REVIEW_ERRORS )) && continue
        do_review "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r '.[]
        | select(.isDraft|not)
        | select([.statusCheckRollup[]? | select(.name=="build")] | any(.conclusion=="SUCCESS"))
        | "\(.number) \(.headRefOid)"')
    fi

    # 3) Fix: first of kim-em's open PRs reviewed-at-head with a 🟡/⛔ rubric.
    if want fix; then
    while read -r pr head refname owner repo; do
        [[ -z "$pr" ]] && break
        [[ "$(ledger_head "$pr")" == "$head" ]] || continue
        [[ "$(ledger_blocking "$pr" "$head")" == "1" ]] || continue
        (( $(counter "$STATE/fix-$pr-${head:0:12}") >= MAX_FIX_ATTEMPTS )) && continue
        (( $(counter "$STATE/fix-pr-$pr") >= MAX_FIX_PR_ATTEMPTS )) && continue
        begin_branch_work "$pr" "$head" "$refname" "$owner" "$repo" || continue
        do_fix "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r --arg me "$ME" '.[] | select(.author.login==$me)
        | "\(.number) \(.headRefOid) \(.headRefName) \(.headRepositoryOwner.login) \(.headRepository.name)"')
    fi

    # 4) Fix red CI: kim-em's open PRs whose "build" check has FAILED at the
    #    current head (not merely pending). Such a PR is never reviewable (review
    #    needs green) and never review-fixable (never reviewed), so without this it
    #    sits red forever while the loop authors around it. Bounded per-head and
    #    per-PR, like the review-fix step, so it can't churn one PR indefinitely.
    if want fix-ci; then
    while read -r pr head refname owner repo; do
        [[ -z "$pr" ]] && break
        (( $(counter "$STATE/ci-$pr-${head:0:12}") >= MAX_CI_ATTEMPTS )) && continue
        (( $(counter "$STATE/ci-pr-$pr") >= MAX_CI_PR_ATTEMPTS )) && continue
        begin_branch_work "$pr" "$head" "$refname" "$owner" "$repo" || continue
        do_fix_ci "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r --arg me "$ME" '.[]
        | select(.author.login==$me)
        | select([.statusCheckRollup[]? | select(.name=="build")
                  | select(.conclusion | IN("FAILURE","ERROR","TIMED_OUT","CANCELLED","STARTUP_FAILURE","ACTION_REQUIRED"))] | any)
        | "\(.number) \(.headRefOid) \(.headRefName) \(.headRepositoryOwner.login) \(.headRepository.name)"')
    fi

    # 4.5) Bump: mathlib master has moved past our pin and no bump PR is open —
    #      author one that bumps to master and fixes the breakage. After all
    #      firefighting (so landing/un-blocking existing PRs is never delayed) but
    #      before roadmap (keeping current beats authoring features on a stale base).
    local bump_tip
    if want bump && bump_tip=$(bump_candidate "$open"); then
        do_bump "$bump_tip"; exit $?
    fi

    # 5) Roadmap: author a new PR — but hold off while the worker already has a
    #    large backlog open. Backpressure stops the queue growing without bound
    #    (and spawning more duplicates) while review/fix/merge drain it; authoring
    #    resumes automatically once the open count falls back below the threshold.
    if want roadmap; then
        local n_mine
        n_mine=$(echo "$open" | jq --arg me "$ME" '[.[] | select(.isDraft|not) | select(.author.login==$me)] | length')
        if (( n_mine >= MAX_OPEN_PRS )); then
            noprogress "roadmap: $n_mine open PRs (>= $MAX_OPEN_PRS) — backpressure, not authoring this round"
        fi
        # Confine authoring to ROADMAP_FOCUS (a single TauCetiRoadmap/ area), or range
        # over all areas when it is empty. Dedup against open PRs (handled in the
        # prompt) keeps successive rounds from repeating the same target within the area.
        do_roadmap "${ROADMAP_FOCUS:-any}"
    else
        # A focused worker (--only without roadmap) found no work in its allowed stages
        # this round. The pre-passes (merge/abandon/dup-sweep) already ran; report
        # no-progress so loop.sh backs off instead of busy-spinning.
        noprogress "no eligible work this round under --only=$ONLY (housekeeping pre-passes ran)"
    fi
}
main
