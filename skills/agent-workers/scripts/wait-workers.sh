#!/bin/sh
# ABOUTME: Blocks until every worker started by run-worker.sh for the given logs has exited.
# ABOUTME: Run it as one background command to get a single completion notification.
# Usage: wait-workers.sh <log-file> [<log-file> ...]
. "$(cd "$(dirname "$0")" && pwd)/state.sh"
run_of() { sed -n 's/^run_id=//p' "$1.run" 2>/dev/null; }
tab=$(printf '\t')
# Wait for the attempts that were current when the wait began, all noted before waiting on any:
# a same-name replacement started while an earlier log was waited on means that attempt ended.
runs=""
for log in "$@"; do
  [ -f "$log.pid" ] || [ -f "$log.exit" ] || { echo "no pid file for $log" >&2; continue; }
  runs="$runs$log$tab$(run_of "$log")
"
done
printf '%s' "$runs" | while IFS="$tab" read -r log run; do
  while { [ -z "$run" ] || [ "$(run_of "$log")" = "$run" ]; } && ops_running "$log"; do sleep "${WAIT_WORKERS_POLL:-10}"; done
  # Its supervisor died before publishing: say so (batches shows it as DIED).
  [ "$(run_of "$log")" != "$run" ] || [ -f "$log.exit" ] || echo "$log: ended without an exit record" >&2
done
echo "all workers finished"
