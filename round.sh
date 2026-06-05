#!/usr/bin/env bash
# round.sh — perform exactly ONE unit of Tau Ceti work, then exit. Called by
# loop.sh once per round.
#
# Priority:
#   1. Review an open TauCeti PR whose current head has not been reviewed yet
#      (and whose CI build is green) — via the `tauceti-review` CLI.
#   2. Fix one of kim-em's open PRs whose latest review (on the current head)
#      requests changes (🟡) or blocks (⛔) — drive an agent to address them.
#   3. Otherwise advance a roadmap target with a new PR.
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
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAUCETI="FormalFrontier/TauCeti"
ROADMAP="FormalFrontier/TauCetiRoadmap"
REVIEW="FormalFrontier/TauCetiReview"
ME="kim-em"
STORE="$HOME/.cache/tauceti-review/store/FormalFrontier__TauCeti/ledger.json"
CHECKOUT="$HERE/checkouts/TauCeti"   # host authoring checkout (used without --bubble)
STATE="$HERE/state"
MAX_FIX_ATTEMPTS=3
mkdir -p "$STATE" "$(dirname "$CHECKOUT")"

log() { echo "$(date '+%F %T') round: $*" >&2; }
die() { log "$*"; exit 1; }

# Args: an optional model override and an optional --bubble (sandbox) flag.
FORCE=""; BUBBLE_MODE=0
while (( $# )); do
    case "$1" in
        --codex|--claude|--deepseek|--minimax) FORCE="${1#--}";;
        --bubble) BUBBLE_MODE=1;;
        *) die "unknown argument: $1 (expected --codex|--claude|--deepseek|--minimax|--bubble)";;
    esac
    shift
done

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
# review uses every subscription model that currently has quota (so it stays
# dual-model when it can). An override pins everything to the one named model.
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
    REVIEWERS=""
    (( OPUS_OK ))  && REVIEWERS="claude"
    (( CODEX_OK )) && REVIEWERS="${REVIEWERS:+$REVIEWERS,}codex"
fi

# Refuse to run two rounds at once: they share a fixed bubble name and the host
# checkout / state dir, and could clobber each other. The lock auto-releases on exit.
exec 9>"$STATE/round.lock"
flock -n 9 || die "another round holds $STATE/round.lock — refusing to run concurrently"

# Validate the ledger once; a malformed ledger would misclassify every PR.
if [[ -f "$STORE" ]] && ! jq empty "$STORE" 2>/dev/null; then
    die "review store ledger is malformed ($STORE) — aborting round"
fi

# ledger_head PR — last head_sha this worker reviewed for PR (empty if none).
ledger_head() {
    [[ -f "$STORE" ]] || { echo ""; return; }
    jq -r --arg pr "$1" '.prs[$pr].rounds[-1].head_sha // ""' "$STORE"
}
# ledger_blocking PR HEAD — "1" if the latest round at exactly HEAD has a
# changes-requested (🟡) or blocked (⛔) rubric.
ledger_blocking() {
    [[ -f "$STORE" ]] || { echo 0; return; }
    jq -r --arg pr "$1" --arg head "$2" '
        (.prs[$pr].rounds[-1] // {}) as $r
        | if ($r.head_sha // "") == $head then
            ([ ($r.states // {}) | to_entries[] | .value ]
             | map(select(. == "blocking_request" or . == "blocking_block")) | length) > 0
          else false end
        | if . then 1 else 0 end' "$STORE"
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
    if [[ "$WORK_MODEL" == codex ]]; then
        ( cd "$cwd" && codex exec --sandbox danger-full-access --skip-git-repo-check "$prompt" )
    elif [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        # OpenRouter model via the `pi` agentic loop: same prompt, full tools so it
        # can build, edit, and push like the others; billed per-token to
        # OPENROUTER_API_KEY (no subscription quota).
        ( cd "$cwd" && "$PI_RUN" openrouter "${OPENROUTER_MODELS[$WORK_MODEL]}" --prompt "$prompt" )
    else
        ( cd "$cwd" && env -u ANTHROPIC_API_KEY claude -p "$prompt" \
              --model opus --dangerously-skip-permissions )
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
BUBBLE="tauceti-worker"   # fixed name; rounds are sequential, so one suffices
# A worker-private bubble data dir, so the sandbox can't inherit ambient
# `[[mounts]]` or a remote/cloud default from the operator's ~/.bubble/config.toml
# — the mount set and runtime are exactly what this script asks for. First use
# builds the worker's git mirrors + Mathlib cache here (slow once, then cached).
export BUBBLE_HOME="${TAUCETI_BUBBLE_HOME:-$HOME/.cache/tauceti-worker/bubble}"

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
    # OpenRouter models need their API key INSIDE the container — there is no proxy
    # for it (unlike GitHub). Stage it 0600 in the round dir; it mounts read-only at
    # /opt/round/openrouter.key and agent_inner_cmd exports it. Gone with the dir.
    if [[ -n "${OPENROUTER_MODELS[$WORK_MODEL]:-}" ]]; then
        ( umask 077; printf '%s' "${OPENROUTER_API_KEY:-}" > "$rounddir/openrouter.key" )
    fi

    # Clear any container left by a previous round that loop.sh's timeout SIGKILLed
    # before --ephemeral could fire (a SIGKILL can't be trapped). Rounds run one at
    # a time (enforced by the flock above), so the fixed name is self-cleaning.
    bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true
    trap 'bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true' EXIT

    local mounts=( --mount "$rounddir:/opt/round:ro" ) m
    for m in "$@"; do mounts+=( --mount "$m" ); done
    local creds=(); while IFS= read -r m; do creds+=( "$m" ); done < <(agent_cred_flags)

    # --local forces the local Incus runtime (a host remote/cloud default would
    # reject the --mount). --github-security allowlist-write-graphql is the minimal
    # level that still lets the agent open a PR and post review-thread replies, all
    # repo-scoped to TauCeti by the proxy; pinning it keeps the worker independent
    # of the host bubble default (a `security.github=off` lockdown still wins and
    # would correctly abort).
    local rc=0
    bubble open "$target" --shell --local --name "$BUBBLE" --ephemeral \
        --github-security allowlist-write-graphql \
        "${mounts[@]}" "${creds[@]}" --command "$(agent_inner_cmd)" || rc=$?

    # Don't rely on --ephemeral's pop alone: if it failed, the container (with the
    # mounted credential) would linger. Pop again before returning.
    bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true
    trap - EXIT
    return $rc
}

# 1. Review --------------------------------------------------------------------
do_review() {
    local pr="$1" head="$2"
    [[ -n "$REVIEWERS" ]] || die "no reviewer models available"
    log "reviewing PR #$pr @ ${head:0:12} (reviewers=$REVIEWERS)"
    uvx --from "git+https://github.com/$REVIEW" tauceti-review "$pr" \
        --post --reviewer "$REVIEWERS" --expect-head "$head"
}

# 2. Fix -----------------------------------------------------------------------
do_fix() {
    local pr="$1" head="$2"
    local key="$STATE/fix-$pr-${head:0:12}" n rc; n=$(counter "$key")
    if (( BUBBLE_MODE )); then
        log "fixing PR #$pr (attempt $((n+1))/$MAX_FIX_ATTEMPTS) with $AGENT in a bubble"
        # bubble checks out the PR branch inside the container; the agent reads the
        # review, fixes, builds, and pushes to the PR branch — all repo-scoped.
        run_in_bubble "$TAUCETI/pull/$pr" \
            "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    else
        prepare_checkout || die "checkout failed"
        ( cd "$CHECKOUT" && gh pr checkout "$pr" ) || die "gh pr checkout #$pr failed"
        log "fixing PR #$pr (attempt $((n+1))/$MAX_FIX_ATTEMPTS) with $AGENT on the host"
        run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"; rc=$?
    fi
    echo $((n+1)) > "$key"   # count the attempt only after it ran
    return $rc
}

# 3. Roadmap -------------------------------------------------------------------
# Both modes stage the read-only roadmap/review reference repos on the host (the
# in-bubble proxy is TauCeti-scoped, so the agent can't fetch them itself) and
# tell the agent where to read them via __ROADMAP_DIR__ / __REVIEW_DIR__.
do_roadmap() {
    local avoid="$1" refs="$STATE/refs"
    fetch_ref "$ROADMAP" "$refs/roadmap" || die "fetch $ROADMAP failed"
    fetch_ref "$REVIEW"  "$refs/review"  || die "fetch $REVIEW failed"
    if (( BUBBLE_MODE )); then
        log "roadmap round with $AGENT in a bubble (avoiding area: $avoid)"
        run_in_bubble "$TAUCETI" \
            "$(fill_prompt "$HERE/prompts/roadmap.md" AVOID "$avoid" AGENT "$AGENT" \
                ROADMAP_DIR /opt/roadmap REVIEW_DIR /opt/review)" \
            "$refs/roadmap:/opt/roadmap:ro" "$refs/review:/opt/review:ro"
    else
        prepare_checkout || die "checkout failed"
        log "roadmap round with $AGENT on the host (avoiding area: $avoid)"
        run_agent "$CHECKOUT" \
            "$(fill_prompt "$HERE/prompts/roadmap.md" AVOID "$avoid" AGENT "$AGENT" \
                ROADMAP_DIR "$refs/roadmap" REVIEW_DIR "$refs/review")"
    fi
}

# ------------------------------------------------------------------------------
main() {
    # One authoritative fetch of open PRs; a GitHub failure aborts the round.
    local open; open=$(gh pr list --repo "$TAUCETI" --state open \
        --json number,headRefOid,isDraft,statusCheckRollup,author) \
        || die "gh pr list failed (GitHub API?) — aborting round, not falling through to authoring"

    # 1) Review: first non-draft, build-green PR whose current head is unreviewed.
    local pr head
    while read -r pr head; do
        [[ -z "$pr" ]] && break
        [[ "$(ledger_head "$pr")" == "$head" ]] && continue
        do_review "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r '.[]
        | select(.isDraft|not)
        | select([.statusCheckRollup[]? | select(.name=="build")] | any(.conclusion=="SUCCESS"))
        | "\(.number) \(.headRefOid)"')

    # 2) Fix: first of kim-em's open PRs reviewed-at-head with a 🟡/⛔ rubric.
    while read -r pr head; do
        [[ -z "$pr" ]] && break
        [[ "$(ledger_head "$pr")" == "$head" ]] || continue
        [[ "$(ledger_blocking "$pr" "$head")" == "1" ]] || continue
        (( $(counter "$STATE/fix-$pr-${head:0:12}") >= MAX_FIX_ATTEMPTS )) && continue
        do_fix "$pr" "$head"; exit $?
    done < <(echo "$open" | jq -r --arg me "$ME" '.[] | select(.author.login==$me) | "\(.number) \(.headRefOid)"')

    # 3) Roadmap: avoid the area (top TauCeti/ subdir) of the most recent PR.
    local recent avoid
    recent=$(gh pr list --repo "$TAUCETI" --state all --limit 1 --json files) \
        || die "gh pr list (recent) failed — aborting round"
    avoid=$(echo "$recent" | jq -r '[.[0].files[]?.path | select(startswith("TauCeti/")) | split("/")[1]] | unique | join(", ")')
    avoid="${avoid:-none}"; echo "$avoid" > "$STATE/last-roadmap-avoid"
    do_roadmap "$avoid"
}
main
