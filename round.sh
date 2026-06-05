#!/usr/bin/env bash
# round.sh — perform exactly ONE unit of Tau Ceti work, then exit. Called by
# loop.sh once per round (only when Codex and/or Opus quota is available).
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
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TAUCETI="FormalFrontier/TauCeti"
ROADMAP="FormalFrontier/TauCetiRoadmap"
REVIEW="FormalFrontier/TauCetiReview"
ME="kim-em"
STORE="$HOME/.cache/tauceti-review/store/FormalFrontier__TauCeti/ledger.json"
STATE="$HERE/state"
MAX_FIX_ATTEMPTS=3
mkdir -p "$STATE"

log() { echo "$(date '+%F %T') round: $*" >&2; }
die() { log "$*"; exit 1; }

# Refuse to run two rounds at once: they share a fixed bubble name and the state/
# dir, and could pop each other's live container. The lock auto-releases on exit.
exec 9>"$STATE/round.lock"
flock -n 9 || die "another round holds $STATE/round.lock — refusing to run concurrently"

# Models. Authoring/fixing prefers Codex (spare Opus); review uses every model
# that currently has quota (so it stays dual-model when it can).
CODEX_OK="${CODEX_OK:-0}"; OPUS_OK="${OPUS_OK:-0}"
if (( CODEX_OK )); then WORK_MODEL=codex; AGENT="Codex"; else WORK_MODEL=claude; AGENT="Claude Code"; fi
REVIEWERS=""
(( OPUS_OK ))  && REVIEWERS="claude"
(( CODEX_OK )) && REVIEWERS="${REVIEWERS:+$REVIEWERS,}codex"

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

# Authoring/fixing runs inside a `bubble` container (kim-em/bubble): the per-round
# checkout, `lake exe cache get` / `lake build` / `lake exe axioms`, and every
# `git`/`gh` call happen IN the container, never on the host. GitHub access is
# mediated by bubble's auth proxy, repo-scoped to FormalFrontier/TauCeti — the
# host's `kim-em` token never enters the container, and a push outside TauCeti is
# rejected by the proxy (not merely flagged by CI after the fact). Only the one
# subscription credential the work model needs is seeded; no host config (notably
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

# agent_cred_flags — bubble flags seeding ONLY the work model's subscription
# credential, with all config and the other subscription kept out of the sandbox.
agent_cred_flags() {
    if [[ "$WORK_MODEL" == codex ]]; then
        printf '%s\n' --codex-credentials --no-codex-config --no-claude-credentials --no-claude-config
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
    # mounted subscription credential) would linger. Pop again before returning.
    bubble pop "$BUBBLE" -f >/dev/null 2>&1 || true
    trap - EXIT
    return $rc
}

# fetch_ref REPO DIR — keep DIR a shallow checkout of REPO's default branch on the
# host (full token), to be mounted read-only into a bubble. The container's GitHub
# proxy is scoped to TauCeti, so the agent can't clone these reference repos itself.
fetch_ref() {
    local repo="$1" dir="$2"
    if [[ -d "$dir/.git" ]]; then
        # These dirs are worker-owned throwaway mirrors (no user work); reset hard
        # and drop any untracked/planted files so the mount is exactly upstream.
        git -C "$dir" fetch -q --depth 1 origin HEAD \
            && git -C "$dir" reset -q --hard FETCH_HEAD \
            && git -C "$dir" clean -fdxq
    else
        rm -rf "$dir"; mkdir -p "$(dirname "$dir")"
        git clone -q --depth 1 "https://github.com/$repo" "$dir"
    fi
}
# fill_prompt FILE KEY VAL [KEY VAL...] — substitute __KEY__ placeholders.
fill_prompt() {
    local f="$1"; shift; local out; out="$(cat "$f")"
    while (( $# )); do out="${out//__${1}__/$2}"; shift 2; done
    printf '%s' "$out"
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
    local key="$STATE/fix-$pr-${head:0:12}" n; n=$(counter "$key")
    log "fixing PR #$pr (attempt $((n+1))/$MAX_FIX_ATTEMPTS) with $AGENT in a bubble"
    # bubble checks out the PR branch inside the container; the agent reads the
    # review, fixes, builds, and pushes to the PR branch — all repo-scoped.
    run_in_bubble "$TAUCETI/pull/$pr" \
        "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"
    local rc=$?
    echo $((n+1)) > "$key"   # count the attempt only after it ran
    return $rc
}

# 3. Roadmap -------------------------------------------------------------------
do_roadmap() {
    local avoid="$1" refs="$STATE/refs"
    # Stage the read-only reference repos on the host; the in-bubble proxy is
    # TauCeti-scoped, so the agent can't fetch these itself. They mount at
    # /opt/roadmap and /opt/review inside the container (see prompts/roadmap.md).
    fetch_ref "$ROADMAP" "$refs/roadmap" || die "fetch $ROADMAP failed"
    fetch_ref "$REVIEW"  "$refs/review"  || die "fetch $REVIEW failed"
    log "roadmap round with $AGENT in a bubble (avoiding area: $avoid)"
    run_in_bubble "$TAUCETI" \
        "$(fill_prompt "$HERE/prompts/roadmap.md" AVOID "$avoid" AGENT "$AGENT")" \
        "$refs/roadmap:/opt/roadmap:ro" "$refs/review:/opt/review:ro"
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
