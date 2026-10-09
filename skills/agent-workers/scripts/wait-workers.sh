#!/bin/sh
# ABOUTME: Blocks until every worker started by run-worker.sh for the given logs has exited.
# ABOUTME: Run it as one background command to get a single completion notification.
# Usage: wait-workers.sh <log-file> [<log-file> ...]
. "$(cd "$(dirname "$0")" && pwd)/state.sh"
run_of() { sed -n 's/^run_id=//p' "$1.run" 2>/dev/null; }
for log in "$@"; do
  [ -f "$log.pid" ] || [ -f "$log.exit" ] || { echo "no pid file for $log" >&2; continue; }
  # Wait for the attempt that was current when the wait began: a same-name replacement means it ended.
  run=$(run_of "$log")
  while { [ -z "$run" ] || [ "$(run_of "$log")" = "$run" ]; } && ops_running "$log"; do sleep "${WAIT_WORKERS_POLL:-10}"; done
  # Its supervisor died before publishing: say so (batches shows it as DIED).
  [ "$(run_of "$log")" != "$run" ] || [ -f "$log.exit" ] || echo "$log: ended without an exit record" >&2
done
echo "all workers finished"
