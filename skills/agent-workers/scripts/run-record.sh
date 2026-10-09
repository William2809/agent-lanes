#!/bin/sh
# ABOUTME: Per-attempt metadata, report and exit records; legacy transcripts read for one release.
ops_plain() { sed -E "s/$(printf '\033')\[[0-9;]*[[:alpha:]]//g" "$1"; }
ops_completed() {
  if [ -f "$1.run" ] || [ -f "$1.exit" ]; then [ -f "$1.exit" ]; return; fi
  grep -q "^\($(printf '\033')\[[0-9;]*m\)*tokens used" "$1" 2>/dev/null
}
ops_reported() {
  if [ -f "$1.run" ] || [ -f "$1.exit" ]; then
    [ -s "$1.last" ] && grep -q '^rc=0 ended=[0-9][0-9]*$' "$1.exit" 2>/dev/null; return
  fi
  ops_completed "$1"
}
# Record mutators run in the caller's process, never a subshell: the lock then names the process
# that writes, so a killed caller leaves a stale lock and no orphan writer.
ops_archive() {
  ops_lock "$1.record.lock" || return 1
  _run_rc=0
  ops_archive_locked "$1" "$2" || _run_rc=1
  ops_unlock "$1.record.lock" || _run_rc=1
  return $_run_rc
}
ops_archive_locked() {
  ! ops_running "$1" || return 1
  for _run_suffix in '' .pid .run .last .exit .delivered; do
    [ ! -f "$1$_run_suffix" ] || mv "$1$_run_suffix" "$2$_run_suffix" || return 1
  done
}
# log preset harness route model effort sandbox dir resume_of; callers resolve the spec once.
# Sets run_id. Under wk (launch_lock set) it refuses once wk no longer holds the launch lock.
ops_run_record() {
  ops_lock "$1.record.lock" || return 1
  _run_rc=0
  ops_run_record_locked "$@" || _run_rc=1
  ops_unlock "$1.record.lock" || _run_rc=1
  return $_run_rc
}
ops_run_record_locked() {
  if [ -n "${launch_lock:-}" ]; then
    _run_owner=$(cat "$launch_lock/pid" 2>/dev/null || true)
    { [ "$_run_owner" = "$$" ] || [ "$_run_owner" = "$PPID" ]; } && ops_pid_live "$_run_owner" ||
      { echo "wk: launch cancelled: the launching wk no longer holds the launch lock" >&2; return 1; }
  fi
  _run_started=$(date +%s)
  for _run_suffix in .pid .exit .delivered; do
    [ ! -e "$1$_run_suffix" ] || ctrash "$1$_run_suffix" >/dev/null || return 1
  done
  # lead_session: the Claude Code session that ran wk (the workers mod wakes it when the run ends).
  printf 'run_id=%s-%s\nstarted=%s\npreset=%s\nharness=%s\nroute=%s\nmodel=%s\neffort=%s\nsandbox=%s\ndir=%s\ntool_rev=%s\nresume_of=%s\nlead_session=%s\n' \
    "$_run_started" "$$" "$_run_started" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$tool_rev" "$9" "${CLAUDE_CODE_SESSION_ID:-}" >"$1.run.new" &&
    mv "$1.run.new" "$1.run" || return 1
  : >"$1.last"
  run_id=$_run_started-$$
  # Accepted: this log's pause rows go; same-named workers of other repos keep theirs.
  ops_pause_retire "$(basename "$1" .log)" "$1" "$8" ||
    echo "wk: could not retire the pause row of $(basename "$1" .log)" >&2
}
# Publish the real worker PID before returning, so launch locks cover registration.
ops_launch() {
  [ -z "${launch_lock:-}" ] || : >"$launch_lock/handoff"
  launch_lock=${launch_lock:-} nohup sh "$_run_scripts/supervise-worker.sh" "$@" </dev/null >/dev/null 2>&1 &
  _run_supervisor=$!
  [ -z "${launch_lock:-}" ] || echo "$_run_supervisor" >"$launch_lock/handoff"
  until [ -f "$1.pid" ] && [ "$(sed -n 's/^run_id=//p' "$1.run" 2>/dev/null)" = "$5" ]; do
    kill -0 "$_run_supervisor" 2>/dev/null || { wait "$_run_supervisor"; return 1; }
    sleep 0.01
  done
}
