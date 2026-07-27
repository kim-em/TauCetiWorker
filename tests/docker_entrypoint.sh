#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

printf '%s\n' '#!/usr/bin/env bash' 'printf "<%s>\\n" "$@"' >"$temporary/tauceti"
chmod +x "$temporary/tauceti"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 \
    TAUCETI_WORKER_ARGS='--only roadmap --roadmap-only "Some Area"' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
expected=$'<work>\n<--loop>\n<--only>\n<roadmap>\n<--roadmap-only>\n<Some Area>'
test "$output" = "$expected"
echo "[OK ] worker options are appended as distinct arguments"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS='' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
test "$output" = $'<work>\n<--loop>'
echo "[OK ] an empty option setting leaves the worker command unchanged"

output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS='--only roadmap' \
    "$ROOT/scripts/docker-entrypoint" ./tauceti doctor
})"
test "$output" = '<doctor>'
echo "[OK ] worker options do not affect other container commands"

marker="$temporary/should-not-exist"
output="$({
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 \
    TAUCETI_WORKER_ARGS="--roadmap-only '\$(touch $marker)'" \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop
})"
test ! -e "$marker"
test "$output" = $'<work>\n<--loop>\n<--roadmap-only>\n<$(touch '"$marker"$')>'
echo "[OK ] worker options are parsed without shell evaluation"

set +e
error="$(
  cd "$temporary"
  TAUCETI_SKIP_GIT_IDENTITY=1 TAUCETI_WORKER_ARGS="'" \
    "$ROOT/scripts/docker-entrypoint" ./tauceti work --loop 2>&1
)"
status=$?
set -e
test "$status" -ne 0
[[ "$error" == tauceti-entrypoint:*TAUCETI_WORKER_ARGS*invalid\ quoting* ]]
[[ "$error" != *Traceback* ]]
echo "[OK ] malformed quoting fails closed with a useful error"
