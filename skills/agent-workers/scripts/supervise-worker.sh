#!/bin/sh
# ABOUTME: Waits for the real worker, preserving its registered PID and every exit status.
set -u
scripts=$(cd "$(dirname "$0")" && pwd)
. "$scripts/state.sh"
log=$1 dir=$2 sandbox=$3 preset=$4 run_id=$5; shift 5
ops_lock "$log.record.lock" || exit 1
trap 'ops_unlock "$log.record.lock"' EXIT
[ -n "$run_id" ] && [ "$(sed -n 's/^run_id=//p' "$log.run" 2>/dev/null)" = "$run_id" ] || exit 0
exec >"$log" 2>&1
tool_rev=$(sed -n 's/^tool_rev=//p' "$log.run")
AGENT_LANES_LOG=$log.$run_id; export AGENT_LANES_LOG
"$@" </dev/null &
pid=$!
ops_register "$pid" "$log" "$dir" "$sandbox" "$preset"
# Clear our launch handoff here, before .pid lets the launcher release its lock: the handoff at
# that path cannot yet belong to a later launch, and no child is left to clear one later.
if [ -n "${launch_lock:-}" ] && [ "$(cat "$launch_lock/handoff" 2>/dev/null)" = "$$" ]; then
  : >"$launch_lock/handoff"
fi
printf '%s\n' "$pid" >"$log.pid.new" && mv "$log.pid.new" "$log.pid"
# A release that failed leaves the lock ours; publication below must not wait on ourselves.
held=0; ops_unlock "$log.record.lock" || held=1
if wait "$pid"; then rc=0; else rc=$?; fi
# Serialize publication with record replacement and archive; an old attempt cannot finish a new one.
[ "$held" = 1 ] && [ "$(cat "$log.record.lock/pid" 2>/dev/null)" = "$$" ] || ops_lock "$log.record.lock" || exit 1
trap 'ops_unlock "$log.record.lock"' EXIT
[ -n "$run_id" ] && [ "$(sed -n 's/^run_id=//p' "$log.run" 2>/dev/null)" = "$run_id" ] || exit 0
[ ! -f "$log.$run_id.last" ] || mv "$log.$run_id.last" "$log.last" || exit 1
printf 'rc=%s ended=%s\n' "$rc" "$(date +%s)" >"$log.exit.new" && mv "$log.exit.new" "$log.exit"
