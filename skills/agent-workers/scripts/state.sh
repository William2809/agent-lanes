#!/bin/sh
# ABOUTME: Shared portable state locks and worker roles; workers.tsv stays four columns.
# Lock callers run in a subshell or install a trap, and hold launch locks through registration.
# A worker PID counts only while it is still a worker process (pids get reused): Codex or harness_run.py.
ops_agent_pid() { ps -p "$1" -o command= 2>/dev/null | grep -qE 'codex|harness_run'; }
ops_pid_live() { case "$1" in ''|*[!0-9]*|0) return 1 ;; esac; kill -0 "$1" 2>/dev/null; }
ops_stat() {
  case "$(uname -s)" in Darwin) stat -f "$1" "$3" 2>/dev/null ;; *) stat -c "$2" "$3" 2>/dev/null ;; esac
}
ops_lock_held() {
  _ops_pid=$(cat "$1/pid" 2>/dev/null || true)
  ops_pid_live "$_ops_pid" && return 0
  [ -d "$1" ] && { [ -z "$_ops_pid" ] || [ -f "$1/handoff" ]; } || return 1
  _ops_age=$1; [ ! -f "$1/handoff" ] || _ops_age=$1/handoff
  _ops_mt=$(ops_stat %m %Y "$_ops_age" || echo 0)
  [ $(( $(date +%s) - _ops_mt )) -lt 10 ]
}
ops_try_lock() (
  if mkdir "$1" 2>/dev/null; then echo $$ >"$1/pid"; exit 0; fi
  _ops_ident=$(ops_stat '%d:%i' '%d:%i' "$1" || true)
  ops_lock_held "$1" && exit 1
  # Serialize stale-lock reclamation so a contender cannot move a newly acquired lock.
  # Reapers use the same recoverable protocol, including their own PID.
  [ -n "$_ops_ident" ] && ops_try_lock "$1/reap" || exit 1
  _ops_current=$(ops_stat '%d:%i' '%d:%i' "$1" || true)
  if [ "$_ops_ident" != "$_ops_current" ] || ops_pid_live "$(cat "$1/pid" 2>/dev/null)"; then
    ops_unlock "$1/reap"; exit 1
  fi
  ctrash "$1" >/dev/null || exit 1
  mkdir "$1" 2>/dev/null || exit 1
  echo $$ >"$1/pid"
)
ops_lock() {
  _ops_wait=0
  until ops_try_lock "$1"; do
    _ops_wait=$((_ops_wait + 1))
    [ "$_ops_wait" -lt 300 ] || { echo "state: lock busy: $1" >&2; return 1; }
    sleep 0.1
  done
}
ops_unlock() { [ "$(cat "$1/pid" 2>/dev/null)" != "$$" ] || ctrash "$1" >/dev/null; }
ops_register() {
  # Role publication precedes the legacy registry: readers need not wait for a Codex header.
  mkdir -p "$HOME/.claude/state"
  _ops_at=$(date +%s)
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$_ops_at" "$1" "$2" "$4" "${5:-}" "$tool_rev" >>"$HOME/.claude/state/worker-roles.tsv"
  printf '%s\t%s\t%s\t%s\n' "$_ops_at" "$1" "$2" "$3" >>"$HOME/.claude/state/workers.tsv"
}
ops_sandbox() {
  _ops_sb=$(awk -F'\t' -v l="$1" '$3==l {s=$4} END{print s}' "$HOME/.claude/state/worker-roles.tsv" 2>/dev/null)
  [ -n "$_ops_sb" ] || _ops_sb=$(sed -n '1,20s/^sandbox: //p' "$1" 2>/dev/null | head -1)
  printf '%s\n' "$_ops_sb"
}
ops_writable() { case "$(ops_sandbox "$1")" in full-access*|danger-full-access*|workspace-write*) return 0 ;; *) return 1 ;; esac; }
ops_writers() (
  # Latest row per transcript, then one row per PID. Resolve repos by common Git directory.
  awk -F'\t' '{last[$3]=$0} END{for (l in last) print last[l]}' "$HOME/.claude/state/workers.tsv" 2>/dev/null |
    sort -rn | awk -F'\t' '!seen[$2]++' |
    while IFS="$(printf '\t')" read -r at pid lg dir; do
      [ -z "${2:-}" ] || [ "$dir" = "$2" ] || continue
      ops_agent_pid "$pid" || continue
      ops_writable "$lg" || continue
      _ops_common=$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || continue
      [ "$_ops_common" = "$1/.git" ] && basename "$lg" .log
    done
)
ops_pending_writers() (
  for _ops_slot in "$HOME/.claude/state/mq/$(basename "$1")/pending/"*; do
    [ -f "$_ops_slot" ] && [ "$_ops_slot" != "${3:-}" ] || continue
    IFS="$(printf '\t')" read -r _ops_pid _ops_dir <"$_ops_slot" || continue
    ops_pid_live "$_ops_pid" || continue
    [ -z "${2:-}" ] || [ "$_ops_dir" = "$2" ] || continue
    basename "$_ops_slot"
  done
)
ops_writer_guard() {
  [ "$3" != read-only ] || return 0
  if [ "${WK_FORCE:-0}" != 1 ]; then
    _ops_busy=$({ ops_writers "$1" "$2"; ops_pending_writers "$1" "$2" "${4:-}"; } | head -1)
    [ -z "$_ops_busy" ] || { echo "wk: $_ops_busy is already writing in $2; one writer per worktree (WK_FORCE=1 overrides)" >&2; return 1; }
  fi
  _ops_max=${WK_MAX_WRITERS:-8}
  case "$_ops_max" in ''|*[!0-9]*) echo "wk: WK_MAX_WRITERS needs a nonnegative integer" >&2; return 2 ;; esac
  if [ "${WK_WRITERS_OK:-0}" != 1 ]; then
    _ops_count=$({ ops_writers "$1"; ops_pending_writers "$1" "" "${4:-}"; } | wc -l | tr -d ' ')
    [ "$_ops_count" -lt "$_ops_max" ] || { echo "wk: $_ops_count writing workers in this repo (limit $_ops_max); not launching (WK_MAX_WRITERS or WK_WRITERS_OK=1 overrides)" >&2; return 1; }
  fi
}
