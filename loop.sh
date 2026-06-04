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
# Each round runs under a hard wall-clock timeout in its own process group, so a
# wedged sub-task is torn down (SIGTERM then SIGKILL) instead of parking the loop
# forever. The round runs in the background and we `wait` on it, recording the
# `timeout` PID, so a terminal Ctrl-C reaches the round (its own process group
# never sees the foreground Ctrl-C otherwise) and stops the whole loop.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_AVAIL="$HOME/.claude/skills/claude-usage/claude-available-model"
CODEX_AVAIL="$HOME/.claude/skills/claude-usage/codex-available-model"
POLL=300             # seconds between quota checks while waiting
ROUND_TIMEOUT=5400   # 90 min hard cap per round (a full author+build can be long)

mkdir -p "$HERE/logs" "$HERE/state"

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
    # Quota: Codex preferred (cheaper), Opus as fallback. Empty = unavailable.
    codex_model=$("$CODEX_AVAIL" 2>/dev/null) || codex_model=""
    claude_model=$("$CLAUDE_AVAIL" --force 2>/dev/null) || claude_model=""
    codex_ok=0; opus_ok=0
    [[ "$codex_model" == gpt-5* ]] && codex_ok=1
    [[ "$claude_model" == "opus" ]] && opus_ok=1   # sonnet does not count

    if (( ! codex_ok && ! opus_ok )); then
        echo "$(date '+%F %T') quota: codex=${codex_model:-none} claude=${claude_model:-none} — sleeping ${POLL}s" >&2
        sleep "$POLL"
        continue
    fi

    echo "$(date '+%F %T') quota OK (codex=$codex_ok opus=$opus_ok) — one round" >&2
    # round.sh picks and performs one task. It reads CODEX_OK / OPUS_OK to choose
    # which subscription model to drive (prefers Codex for authoring/fixing; uses
    # whichever models are available for review).
    CODEX_OK="$codex_ok" OPUS_OK="$opus_ok" run_round task "$HERE/round.sh"
done
