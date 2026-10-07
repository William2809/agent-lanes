#!/bin/sh
# ABOUTME: Waits for the real worker, preserving its registered PID and every exit status.
set -u
scripts=$(cd "$(dirname "$0")" && pwd)
. "$scripts/state.sh"
log=$1 dir=$2 sandbox=$3 preset=$4; shift 4
run_id=$(sed -n 's/^run_id=//p' "$log.run")
tool_rev=$(sed -n 's/^tool_rev=//p' "$log.run")
AGENT_LANES_LOG=$log; export AGENT_LANES_LOG
"$@" </dev/null &
pid=$!
ops_register "$pid" "$log" "$dir" "$sandbox" "$preset"
printf '%s\n' "$pid" >"$log.pid.new" && mv "$log.pid.new" "$log.pid"
if wait "$pid"; then rc=0; else rc=$?; fi
# Serialize publication with record replacement and archive; an old attempt cannot finish a new one.
ops_lock "$log.record.lock" || exit 1
trap 'ops_unlock "$log.record.lock"' EXIT
[ -n "$run_id" ] && [ "$(sed -n 's/^run_id=//p' "$log.run" 2>/dev/null)" = "$run_id" ] || exit 0
printf 'rc=%s ended=%s\n' "$rc" "$(date +%s)" >"$log.exit.new" && mv "$log.exit.new" "$log.exit"
