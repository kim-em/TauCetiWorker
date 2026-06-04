#!/usr/bin/env bash
# round.sh — perform exactly ONE unit of Tau Ceti work, then exit. Called by
# loop.sh once per round (only when Codex and/or Opus quota is available).
#
# Priority:
#   1. Review an open TauCeti PR whose current head has not been reviewed yet
#      (and whose CI build is green) — via the `tauceti-review` CLI.
#   2. Fix one of kim-em's open PRs whose latest review (on the current head)
#      requests changes or blocks — drive an agent to address the findings.
#   3. Otherwise advance a roadmap target with a new PR — drive an agent to
#      author a small, green, on-roadmap contribution (avoiding the area of the
#      most recently opened PR).
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
mkdir -p "$STATE" "$(dirname "$CHECKOUT")"

log() { echo "$(date '+%F %T') round: $*" >&2; }

# Which models to use. Authoring/fixing prefers Codex (spare Opus); review uses
# every model that currently has quota (so it stays dual-model when possible).
CODEX_OK="${CODEX_OK:-0}"; OPUS_OK="${OPUS_OK:-0}"
if (( CODEX_OK )); then WORK_MODEL=codex; else WORK_MODEL=claude; fi
REVIEWERS=""
(( OPUS_OK ))  && REVIEWERS="claude"
(( CODEX_OK )) && REVIEWERS="${REVIEWERS:+$REVIEWERS,}codex"

# ledger_head PR — last head_sha this worker reviewed for PR (empty if none).
ledger_head() {
    [[ -f "$STORE" ]] || { echo ""; return; }
    jq -r --arg pr "$1" '.prs[$pr].rounds[-1].head_sha // ""' "$STORE" 2>/dev/null
}
# ledger_blocking PR HEAD — "1" if the latest round at HEAD requests changes/blocks.
ledger_blocking() {
    [[ -f "$STORE" ]] || { echo 0; return; }
    jq -r --arg pr "$1" --arg head "$2" '
        (.prs[$pr].rounds[-1] // {}) as $r
        | if ($r.head_sha // "") | startswith($head[0:12]) then
            ([ $r.states // {} | to_entries[] | .value ]
             | map(select(. == "blocking_request" or . == "blocking_block")) | length) > 0
          else false end
        | if . then 1 else 0 end' "$STORE" 2>/dev/null || echo 0
}

# Make $CHECKOUT a clean checkout of TauCeti `main` (clone once; reuses .lake).
prepare_checkout() {
    if [[ ! -d "$CHECKOUT/.git" ]]; then
        log "cloning $TAUCETI → $CHECKOUT (first run)"
        git clone -q "https://github.com/$TAUCETI" "$CHECKOUT" || return 1
    fi
    git -C "$CHECKOUT" fetch -q origin || return 1
    git -C "$CHECKOUT" switch -q main 2>/dev/null || git -C "$CHECKOUT" checkout -q -B main origin/main
    git -C "$CHECKOUT" reset -q --hard origin/main
    git -C "$CHECKOUT" clean -fdq -e .lake
}

# run_agent CWD PROMPT — drive the chosen subscription model on a coding task in
# CWD with full tool access. Bills the subscription, not the API.
run_agent() {
    local cwd="$1" prompt="$2"
    if [[ "$WORK_MODEL" == codex ]]; then
        ( cd "$cwd" && codex exec --sandbox danger-full-access --skip-git-repo-check "$prompt" )
    else
        # Opus specifically (its quota is what we gated on); -u ANTHROPIC_API_KEY
        # so it bills the Max subscription, not the API.
        ( cd "$cwd" && env -u ANTHROPIC_API_KEY claude -p "$prompt" \
              --model opus --dangerously-skip-permissions )
    fi
}

# ---------------------------------------------------------------------------
# 1. Review: an open, non-draft PR whose CI build is green and whose current
#    head has not been reviewed yet.
# ---------------------------------------------------------------------------
review_candidate() {
    gh pr list --repo "$TAUCETI" --state open --json number,headRefOid,isDraft,statusCheckRollup \
        --jq '.[] | select(.isDraft|not)
              | select([.statusCheckRollup[]? | select(.name=="build")]
                        | any(.conclusion=="SUCCESS"))
              | "\(.number) \(.headRefOid)"' 2>/dev/null \
    | while read -r pr head; do
        [[ "$(ledger_head "$pr")" == "$head" ]] && continue   # already reviewed at this head
        echo "$pr $head"; break
      done
}

do_review() {
    local pr="$1" head="$2"
    [[ -n "$REVIEWERS" ]] || { log "no reviewer models available; skip review"; return 1; }
    log "reviewing PR #$pr @ ${head:0:12} with reviewers=$REVIEWERS"
    uvx --from "git+https://github.com/$REVIEW" tauceti-review "$pr" \
        --post --reviewer "$REVIEWERS" --expect-head "$head"
}

# ---------------------------------------------------------------------------
# 2. Fix: one of kim-em's open PRs whose latest review (at the current head)
#    requests changes or blocks. Guard against re-attempting the same head
#    endlessly (mark each attempt; skip a head after MAX_FIX_ATTEMPTS).
# ---------------------------------------------------------------------------
MAX_FIX_ATTEMPTS=3
fix_candidate() {
    gh pr list --repo "$TAUCETI" --state open --author "$ME" --json number,headRefOid \
        --jq '.[] | "\(.number) \(.headRefOid)"' 2>/dev/null \
    | while read -r pr head; do
        [[ "$(ledger_head "$pr")" == "$head" ]] || continue          # not reviewed at this head yet
        [[ "$(ledger_blocking "$pr" "$head")" == "1" ]] || continue  # review is not blocking
        local n; n=$(cat "$STATE/fix-$pr-${head:0:12}" 2>/dev/null || echo 0)
        (( n >= MAX_FIX_ATTEMPTS )) && continue
        echo "$pr $head"; break
      done
}

do_fix() {
    local pr="$1" head="$2"
    prepare_checkout || { log "checkout failed"; return 1; }
    local branch; branch=$(gh pr view "$pr" --repo "$TAUCETI" --json headRefName --jq .headRefName)
    git -C "$CHECKOUT" fetch -q origin "$branch" \
        && git -C "$CHECKOUT" switch -q -C "$branch" FETCH_HEAD || { log "branch fetch failed"; return 1; }
    local n; n=$(cat "$STATE/fix-$pr-${head:0:12}" 2>/dev/null || echo 0)
    echo $((n+1)) > "$STATE/fix-$pr-${head:0:12}"
    log "fixing PR #$pr (branch $branch, attempt $((n+1))/$MAX_FIX_ATTEMPTS) with $WORK_MODEL"
    run_agent "$CHECKOUT" "$(sed "s/__PR__/$pr/g" "$HERE/prompts/fix.md")"
}

# ---------------------------------------------------------------------------
# 3. Roadmap: author a new PR advancing a target, avoiding the area of the most
#    recently opened PR (so the worker spreads across the roadmaps).
# ---------------------------------------------------------------------------
do_roadmap() {
    prepare_checkout || { log "checkout failed"; return 1; }
    # Area of the most recently opened PR = first path segment under TauCeti/.
    local avoid
    avoid=$(gh pr list --repo "$TAUCETI" --state all --limit 1 --json files \
            --jq '[.[0].files[].path | select(startswith("TauCeti/")) | split("/")[1]] | unique | join(", ")' 2>/dev/null)
    avoid="${avoid:-none}"
    echo "$avoid" > "$STATE/last-roadmap-avoid"
    log "roadmap round with $WORK_MODEL (avoiding area: $avoid)"
    run_agent "$CHECKOUT" "$(sed "s/__AVOID__/$avoid/g" "$HERE/prompts/roadmap.md")"
}

# ---------------------------------------------------------------------------
main() {
    local pr head
    read -r pr head < <(review_candidate); [[ -n "${pr:-}" ]] && { do_review "$pr" "$head"; exit $?; }
    read -r pr head < <(fix_candidate);    [[ -n "${pr:-}" ]] && { do_fix "$pr" "$head"; exit $?; }
    do_roadmap
}
main
