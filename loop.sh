#!/usr/bin/env bash
# loop.sh — Drive Tau Ceti work autonomously, but only while subscription quota
# permits. Each round does exactly ONE unit of work, chosen by round.sh:
#
#   1. review an open TauCeti PR that needs a review, else
#   2. fix one of kim-em's open PRs whose review requests changes, else
#   3. start a new PR advancing a roadmap target.
#
# Model policy (mirrors ../lean-eval-knill/loop.sh): prefer Codex when its quota
# is OK, to spare the more precious Claude Max Opus quota; fall back to Opus when
# Codex is exhausted but Opus has quota; sleep and re-check when neither is
# available. (Sonnet-only does NOT count — this worker wants Opus or Codex.)
#
# Account selection: before measuring Claude quota we run `swap-account best
# --force` so we read (and then run under) whichever Claude account has the most
# quota. Without this we'd read whatever account happens to be active — which can
# be a near-exhausted one (→ sonnet → sleep) while another sits idle with full
# Opus. Codex quota is cache-backed; if the cache is stale (exit 2) we refresh it
# once and re-check rather than treating "unknown" as "exhausted".
#
# An explicit override flag pins the loop to one model:
#   --codex / --claude   wait on ONLY that subscription's quota, then run.
#   --deepseek / --minimax   run an OpenRouter model through the `pi` agent
#                            (pay-per-token, no subscription quota to wait on).
# DeepSeek/MiniMax run ONLY when their flag is passed — never auto-dispatched.
#
# Sandboxing: by default authoring/fixing runs on the HOST. Pass --bubble to run
# each authoring/fixing round inside a `bubble` container instead (repo-scoped to
# TauCeti). The flag is forwarded to round.sh and combines with any model flag.
#
# Each round runs under a hard wall-clock timeout in its own process group, so a
# wedged sub-task is torn down (SIGTERM then SIGKILL) instead of parking the loop
# forever. The round runs in the background and we `wait` on it, recording the
# `timeout` PID, so a terminal Ctrl-C reaches the round (its own process group
# never sees the foreground Ctrl-C otherwise) and stops the whole loop.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_AVAIL="$HOME/.claude/skills/claude-usage/claude-available-model"
CODEX_AVAIL="$HOME/.claude/skills/claude-usage/codex-available-model"
CODEX_REFRESH="$HOME/.claude/skills/claude-usage/codex-usage-refresh"
SWAP="$HOME/.claude/swap-account"   # picks the Claude account with the most quota
POLL=300             # seconds between quota checks while waiting
ROUND_TIMEOUT=5400   # 90 min hard cap per round (a full author+build can be long)
INTERROUND=20        # min gap between rounds, so a no-op round can't busy-loop

# OpenRouter providers driven through `pi` (kept in sync with round.sh's map).
# They are pay-per-token, so they bypass the subscription-quota wait entirely.
OPENROUTER_PROVIDERS=" deepseek minimax "
is_openrouter() { [[ "$OPENROUTER_PROVIDERS" == *" $1 "* ]]; }

# Optional explicit model override and/or --bubble (sandbox); forwarded to round.sh.
FORCE=""; BUBBLE_MODE=0
while (( $# )); do
    case "$1" in
        --codex|--claude|--deepseek|--minimax) FORCE="${1#--}";;
        --bubble) BUBBLE_MODE=1;;
        -h|--help) echo "usage: $0 [--codex|--claude|--deepseek|--minimax] [--bubble]"; exit 0;;
        *) echo "unknown argument: $1 (expected --codex|--claude|--deepseek|--minimax|--bubble)" >&2; exit 64;;
    esac
    shift
done
# Args forwarded to round.sh each round (model override and/or --bubble).
ROUND_ARGS=()
[[ -n "$FORCE" ]] && ROUND_ARGS+=( "--$FORCE" )
(( BUBBLE_MODE )) && ROUND_ARGS+=( --bubble )

mkdir -p "$HERE/logs" "$HERE/state"

# Fail loudly at startup if the environment can't do a round, rather than
# sleeping forever or silently falling through to authoring.
preflight() {
    local bad=0 t
    for t in gh git jq uvx; do
        command -v "$t" >/dev/null || { echo "preflight: missing '$t' on PATH" >&2; bad=1; }
    done
    # Sandbox vs host: --bubble runs the round in a container; host authoring
    # builds with lake on the host.
    if (( BUBBLE_MODE )); then
        command -v bubble >/dev/null || { echo "preflight: --bubble needs the 'bubble' CLI on PATH (kim-em/bubble)" >&2; bad=1; }
    else
        command -v lake >/dev/null || { echo "preflight: host authoring needs an elan/lake toolchain on PATH (or pass --bubble)" >&2; bad=1; }
    fi
    if [[ -n "$FORCE" ]] && is_openrouter "$FORCE"; then
        # OpenRouter path: needs the key always (host injects it; --bubble mounts it),
        # and `pi` on the host — the host path drives it, and bubble bakes it into the
        # container image when present (same pattern as claude/codex).
        [[ -n "${OPENROUTER_API_KEY:-}" ]] || { echo "preflight: --$FORCE needs OPENROUTER_API_KEY exported (it is interactive-shell-only by default)" >&2; bad=1; }
        command -v pi >/dev/null || { echo "preflight: --$FORCE needs the 'pi' agent on PATH (~/.claude/skills/pi/)" >&2; bad=1; }
    else
        # Subscription path (auto, or forced codex/claude): need the model CLI and
        # the quota scripts that gate the wait. (Even in --bubble mode the host
        # sources the subscription credential and reads quota here.)
        if [[ "$FORCE" == codex ]]; then
            command -v codex >/dev/null || { echo "preflight: --codex needs the 'codex' CLI on PATH" >&2; bad=1; }
        elif [[ "$FORCE" == claude ]]; then
            command -v claude >/dev/null || { echo "preflight: --claude needs the 'claude' CLI on PATH" >&2; bad=1; }
        else
            command -v claude >/dev/null || command -v codex >/dev/null \
                || { echo "preflight: need claude and/or codex on PATH" >&2; bad=1; }
        fi
        [[ -x "$CLAUDE_AVAIL" && -x "$CODEX_AVAIL" ]] \
            || { echo "preflight: quota scripts not found under ~/.claude/skills/claude-usage/" >&2; bad=1; }
    fi
    gh auth status >/dev/null 2>&1 || { echo "preflight: gh is not authenticated (run 'gh auth login')" >&2; bad=1; }
    (( bad )) && { echo "preflight failed — fix the above and re-run" >&2; exit 1; }
}
preflight

round_tpid=""
trap 'echo "$(date "+%F %T") loop.sh: interrupted — stopping round and exiting" >&2;
      if [[ -n $round_tpid ]]; then kill -TERM "$round_tpid" 2>/dev/null; wait "$round_tpid" 2>/dev/null; fi;
      exit 130' INT TERM

# run_round LABEL CMD [ARGS...] — run under a timeout, logging to a timestamped
# file; back off on failure/timeout. The command stays the direct child of
# `timeout` (process-group teardown), and runs in the background so the trap can
# fire.
run_round() {
    local label="$1"; shift
    local log="$HERE/logs/${label}-$(date '+%Y%m%d-%H%M%S').log"
    local rc=0
    echo "$(date '+%F %T') round[$label] → $log" >&2
    timeout --kill-after=30s "$ROUND_TIMEOUT" "$@" >"$log" 2>&1 &
    round_tpid=$!
    wait "$round_tpid" || rc=$?
    round_tpid=""
    if (( rc == 124 || rc == 137 )); then
        echo "$(date '+%F %T') round[$label] timed out after ${ROUND_TIMEOUT}s — pausing 60s" >&2
        sleep 60
    elif (( rc != 0 )); then
        echo "$(date '+%F %T') round[$label] exited rc=$rc (see $log) — pausing 60s" >&2
        sleep 60
    fi
}

while true; do
    # Forced OpenRouter model (DeepSeek/MiniMax via pi): pay-per-token, so there
    # is no subscription quota to wait on — run a round every cycle. Cost is
    # bounded by you having chosen to run with --$FORCE.
    if [[ -n "$FORCE" ]] && is_openrouter "$FORCE"; then
        echo "$(date '+%F %T') forced $FORCE (OpenRouter via pi)${BUBBLE_MODE:+ [bubble]} — one round" >&2
        run_round task "$HERE/round.sh" "${ROUND_ARGS[@]}"
        sleep "$INTERROUND"
        continue
    fi

    # Quota: Codex preferred (cheaper), Opus as fallback. Empty = unavailable.

    # Codex is cache-backed. Exit 2 means the cache is stale/missing ("unknown",
    # NOT exhausted) — the refresh timer fires less often than the cache TTL, so
    # this is the common case mid-cycle. Refresh once and re-check before giving
    # up, otherwise live Codex quota reads as "none" for half of every cycle.
    codex_model=$("$CODEX_AVAIL" 2>/dev/null); codex_rc=$?
    if (( codex_rc == 2 )) && [[ -x "$CODEX_REFRESH" ]]; then
        "$CODEX_REFRESH" >/dev/null 2>&1 || true
        codex_model=$("$CODEX_AVAIL" 2>/dev/null) || codex_model=""
    fi

    # Switch to the Claude account with the most remaining quota BEFORE measuring
    # it, so the quota we read is the quota the round will actually run under. A
    # failure here is non-fatal: we fall back to whichever account is active.
    if [[ -x "$SWAP" ]]; then
        "$SWAP" best --force >/dev/null 2>&1 \
            || echo "$(date '+%F %T') swap-account best failed — using active account" >&2
    fi
    claude_model=$("$CLAUDE_AVAIL" --force 2>/dev/null) || claude_model=""

    codex_ok=0; opus_ok=0
    [[ "$codex_model" == gpt-5* ]] && codex_ok=1
    [[ "$claude_model" == "opus" ]] && opus_ok=1   # sonnet does not count

    # A forced subscription model waits on ONLY its own quota.
    [[ "$FORCE" == codex ]]  && opus_ok=0
    [[ "$FORCE" == claude ]] && codex_ok=0

    if (( ! codex_ok && ! opus_ok )); then
        echo "$(date '+%F %T') quota: codex=${codex_model:-none} claude=${claude_model:-none}${FORCE:+ (forced $FORCE)} — sleeping ${POLL}s" >&2
        sleep "$POLL"
        continue
    fi

    echo "$(date '+%F %T') quota OK (codex=$codex_ok opus=$opus_ok)${FORCE:+ forced $FORCE}${BUBBLE_MODE:+ [bubble]} — one round" >&2
    # round.sh picks and performs one task. Without an override it reads CODEX_OK /
    # OPUS_OK to choose which subscription model to drive (prefers Codex for
    # authoring/fixing; uses whichever models are available for review); with an
    # override it pins everything to the one named model. --bubble (if set) is in
    # ROUND_ARGS and sandboxes the authoring/fixing round.
    if [[ -n "$FORCE" ]]; then
        run_round task "$HERE/round.sh" "${ROUND_ARGS[@]}"
    else
        CODEX_OK="$codex_ok" OPUS_OK="$opus_ok" run_round task "$HERE/round.sh" "${ROUND_ARGS[@]}"
    fi
    sleep "$INTERROUND"   # floor between rounds; a no-op round can't tight-loop
done
