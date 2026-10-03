#!/bin/sh
# ABOUTME: Blocks until every worker started by run-worker.sh for the given logs has exited.
# ABOUTME: Run it as one background command to get a single completion notification.
# Usage: wait-workers.sh <log-file> [<log-file> ...]
for log in "$@"; do
  pid=$(cat "$log.pid" 2>/dev/null) || { echo "no pid file for $log" >&2; continue; }
  while kill -0 "$pid" 2>/dev/null; do sleep 10; done
done
echo "all workers finished"
