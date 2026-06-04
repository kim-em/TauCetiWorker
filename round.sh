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
CHECKOUT="$HERE/checkouts/TauCeti"
STATE="$HERE/state"
MAX_FIX_ATTEMPTS=3
mkdir -p "$STATE" "$(dirname "$CHECKOUT")"

log() { echo "$(date '+%F %T') round: $*" >&2; }
die() { log "$*"; exit 1; }

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

# run_agent CWD PROMPT — drive the chosen subscription model on a coding task in
# CWD with full tool access, billed to the subscription. Returns the agent's rc.
run_agent() {
    local cwd="$1" prompt="$2"
    if [[ "$WORK_MODEL" == codex ]]; then
        ( cd "$cwd" && codex exec --sandbox danger-full-access --skip-git-repo-check "$prompt" )
    else
        ( cd "$cwd" && env -u ANTHROPIC_API_KEY claude -p "$prompt" \
              --model opus --dangerously-skip-permissions )
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
    prepare_checkout || die "checkout failed"
    ( cd "$CHECKOUT" && gh pr checkout "$pr" ) || die "gh pr checkout #$pr failed"
    local key="$STATE/fix-$pr-${head:0:12}" n; n=$(counter "$key")
    log "fixing PR #$pr (attempt $((n+1))/$MAX_FIX_ATTEMPTS) with $AGENT"
    run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/fix.md" PR "$pr" AGENT "$AGENT")"
    local rc=$?
    echo $((n+1)) > "$key"   # count the attempt only after it ran
    return $rc
}

# 3. Roadmap -------------------------------------------------------------------
do_roadmap() {
    local avoid="$1"
    prepare_checkout || die "checkout failed"
    log "roadmap round with $AGENT (avoiding area: $avoid)"
    run_agent "$CHECKOUT" "$(fill_prompt "$HERE/prompts/roadmap.md" AVOID "$avoid" AGENT "$AGENT")"
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
