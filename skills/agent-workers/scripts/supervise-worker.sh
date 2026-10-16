#!/bin/sh
# ABOUTME: Waits for the real worker, preserving its registered PID and every exit status.
set -u
scripts=$(cd "$(dirname "$0")" && pwd)
. "$scripts/state.sh"
. "$scripts/run-record.sh"
log=$1 dir=$2 sandbox=$3 preset=$4 run_id=$5; shift 5
ops_lock "$log.record.lock" || exit 1
trap 'ops_unlock "$log.record.lock"' EXIT
[ -n "$run_id" ] && [ "$(sed -n 's/^run_id=//p' "$log.run" 2>/dev/null)" = "$run_id" ] || exit 0
exec >"$log" 2>&1
tool_rev=$(sed -n 's/^tool_rev=//p' "$log.run")
AGENT_LANES_LOG=$log.$run_id; export AGENT_LANES_LOG
exe=$1; retries=0; held=1
rv() { sed -n "s/^$1=//p" "$log.run" | head -1; }
hv() { ops_plain "$log" | sed -n "1,20s/^$1: //p" | head -1; }
while :; do
  first_line=$(( $(wc -l <"$log") + 1 ))
  "$@" </dev/null &
  pid=$!
  # Registration replaces all identities atomically; this supervisor owns the backoff too.
  ops_register "$pid" "$log" "$dir" "$sandbox" "$preset"
  # Clear the launch handoff before publishing the first worker PID.
  if [ "$retries" = 0 ] && [ -n "${launch_lock:-}" ] && [ "$(cat "$launch_lock/handoff" 2>/dev/null)" = "$$" ]; then
    : >"$launch_lock/handoff"
  fi
  printf '%s\n' "$pid" >"$log.pid.new" && mv "$log.pid.new" "$log.pid"
  held=0; ops_unlock "$log.record.lock" || held=1
  if wait "$pid"; then rc=0; else rc=$?; fi
  # Only a failed provider attempt can resume. Signals and real errors stay terminal.
  [ "$rc" -gt 0 ] && [ "$rc" -lt 126 ] && [ "$retries" -lt 3 ] || break
  sed -n "${first_line},\$p" "$log" | tail -20 |
    sed -E "s/$(printf '\033')\[[0-9;]*[[:alpha:]]//g" |
    awk '/^(ERROR: |Reconnecting\.\.\. |We.re currently experiencing high demand)/ { last=$0 } END { print last }' |
    grep -Eq "^(ERROR: .*)?We're currently experiencing high demand|^Reconnecting\.\.\. 5/5" || break
  sid=$(hv 'session id'); [ -n "$sid" ] || break
  retries=$((retries + 1))
  case "$retries" in 1) delay=120 ;; 2) delay=300 ;; 3) delay=600 ;; esac
  echo "worker: capacity resume $retries/3 delay=${delay}s rc=$rc"
  sleep "$delay" || break
  [ "$held" = 1 ] && [ "$(cat "$log.record.lock/pid" 2>/dev/null)" = "$$" ] || ops_lock "$log.record.lock" || exit 1
  held=1
  [ "$(rv run_id)" = "$run_id" ] || exit 0
  ops_is_paused "$(basename "$log" .log)" "$log" && break
  : >"$log.$run_id.last"
  msg='You were cut off by a provider capacity error. Continue the same task. Check git status/diff, finish the remaining work and checks, and give the requested final report.'
  if [ "$(rv harness)" = codex ]; then
    set -- "$exe" exec
    route=$(rv route)
    [ "$route" = codex ] || [ -z "$route" ] || set -- "$@" --profile "$route"
    set -- "$@" --color never resume -o "$log.$run_id.last" --model "$(rv model)" \
      -c "model_reasoning_effort=\"$(rv effort)\"" -c "sandbox_mode=\"$sandbox\""
    provider=$(hv provider)
    [ -z "$provider" ] || set -- "$@" -c "model_provider=\"$provider\""
    set -- "$@" --skip-git-repo-check "$sid" "$msg"
  else
    set -- python3 "$scripts/harness_run.py" resume "$log" "$msg" "$(rv effort)"
  fi
done
# Serialize publication with record replacement and archive; an old attempt cannot finish a new one.
[ "$held" = 1 ] && [ "$(cat "$log.record.lock/pid" 2>/dev/null)" = "$$" ] || ops_lock "$log.record.lock" || exit 1
trap 'ops_unlock "$log.record.lock"' EXIT
[ -n "$run_id" ] && [ "$(sed -n 's/^run_id=//p' "$log.run" 2>/dev/null)" = "$run_id" ] || exit 0
[ ! -f "$log.$run_id.last" ] || mv "$log.$run_id.last" "$log.last" || exit 1
printf 'rc=%s ended=%s\n' "$rc" "$(date +%s)" >"$log.exit.new" && mv "$log.exit.new" "$log.exit"
