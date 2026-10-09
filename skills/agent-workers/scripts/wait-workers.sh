#!/bin/sh
# ABOUTME: Blocks until every worker started by run-worker.sh for the given logs has exited.
# ABOUTME: Run it as one background command to get a single completion notification.
# Usage: wait-workers.sh <log-file> [<log-file> ...]
. "$(cd "$(dirname "$0")" && pwd)/state.sh"
for log in "$@"; do
  [ -f "$log.pid" ] || [ -f "$log.exit" ] || { echo "no pid file for $log" >&2; continue; }
  while ops_running "$log"; do sleep 10; done
done
echo "all workers finished"
