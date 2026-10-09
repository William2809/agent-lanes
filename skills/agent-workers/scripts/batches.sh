#!/bin/sh
# ABOUTME: Short tracker of what needs the lead: workers running / DIED / STALLED, dead pipelines,
# ABOUTME: and wt-dev worktrees with work not yet merged. Finished items collapse to one count.
# Usage: batches            only what needs attention (cheap to run often)
#        batches --all      every worker of the last 24h
#        batches pause [--stalled] [NAME…]  pause running workers; --stalled includes STALLED
#        batches resume [NAME…] resume named workers (no names: all paused)
#        batches ack NAME…  mark a DIED/STALLED worker or pipeline as handled
#        batches wait [NAME…] block until those workers exit (no names: until any running one exits), one "NAME finished" line each
#                           (run as one background command for a notification)
#        batches report NAME…  final report of finished workers (report.sh on their logs)
#        batches overlap   unlanded worktrees that touch the same files (future land conflicts)
# Exit 3 when something needs the lead (DIED, STALLED). Run inside the repo for worktrees.
. "$(dirname "$(readlink -f "$0")")/state.sh"
. "$(dirname "$(readlink -f "$0")")/run-record.sh"
state="$HOME/.claude/state"; reg="$state/workers.tsv"; acked="$state/acked"; paused="$state/paused-workers.tsv"
mkdir -p "$state"; touch "$acked"
# Import the old dock queue once; keep the source in trash.
if [ -s "$state/dock/paused.txt" ]; then
  cat "$state/dock/paused.txt" >>"$paused" && ctrash "$state/dock/paused.txt" >/dev/null || exit 1
fi
is_paused() { ops_is_paused "$1" "${2:-}"; }
if [ "${1:-}" = "ack" ]; then shift; for n in "$@"; do echo "$n" >>"$acked"; done; exit 0; fi
# Latest registry row for a worker name (log basename without .log): "pid<TAB>log".
row() { awk -F'\t' -v n="$1" -v d="${2:-}" '{b=$3; sub(/.*\//,"",b); sub(/\.log$/,"",b)} b==n && (d=="" || $4==d) {r=$2"\t"$3} END {if (r) print r}' "$reg"; }
# pause_one NAME PID LOG DIR: runs in this process, so the record lock names the process that writes.
pause_one() {
  ops_lock "$3.record.lock" || return 1
  _p_rc=0
  if [ -f "$3.exit" ]; then :
  elif ! ops_worker_live "$2" "$3" signal; then
    echo "cannot pause $1: worker identity unavailable or changed" >&2
  elif kill "$2" 2>/dev/null; then
    # The attempt's own record keeps the pause after a resume archives it (workers mod reads it).
    { cp "$3.run" "$3.run.new" && printf 'paused_at=%s\n' "$(date +%s)" >>"$3.run.new" &&
      mv "$3.run.new" "$3.run"; } || echo "batches: could not mark $1 paused in its record" >&2
    if ops_pause_record "$1" "$4" "$(sed -n 's/^run_id=//p' "$3.run")" "$3"; then echo "paused $1"; else _p_rc=1; fi
  fi
  ops_unlock "$3.record.lock" || _p_rc=1
  return $_p_rc
}
case "${1:-}" in
  pause) shift
    stalled=0; [ "${1:-}" = --stalled ] && { stalled=1; shift; }
    [ -s "$reg" ] || exit 0
    # --all keeps acknowledged STALLED workers available for an explicit pause.
    live=$("$0" --all 2>/dev/null | awk -v stalled="$stalled" '$2=="running" || (stalled && $2=="STALLED"){print $1}')
    [ $# -eq 0 ] && set -- $live
    for n in "$@"; do
      printf '%s\n' "$live" | grep -qx "$n" || { echo "$n is not running" >&2; continue; }
      r=$(row "$n"); [ -n "$r" ] || continue
      pid=${r%%"$(printf '\t')"*}
      kill -0 "$pid" 2>/dev/null || continue
      log=${r#*"$(printf '\t')"}
      is_paused "$n" "$log" && continue
      dir=$(awk -F'\t' -v target="$log" '$3==target {d=$4} END {print d}' "$reg")
      pause_one "$n" "$pid" "$log" "$dir" || true
    done
    exit 0 ;;
  resume) shift
    [ -s "$paused" ] || { echo "no paused workers"; exit 0; }
    snapshot="$paused.$(date +%Y%m%d-%H%M%S).$$"
    cp "$paused" "$snapshot" || exit 1
    sort -u "$snapshot" | while IFS="$(printf '\t')" read -r n dir run_id plog; do
      selected=0; [ $# -eq 0 ] && selected=1
      for wanted in "$@"; do [ "$wanted" = "$n" ] && selected=1; done
      [ "$selected" -eq 1 ] || continue
      # Another resume may have accepted a new attempt since the snapshot.
      # Rows name their log; a bare name could pick a same-named worker of another repo.
      if [ -n "$plog" ]; then l=$plog; r=$plog; else r=$(row "$n" "$dir"); l=${r#*"$(printf '\t')"}; fi
      if [ -n "$run_id" ] && [ -n "$r" ] && [ "$(sed -n 's/^run_id=//p' "$l.run" 2>/dev/null)" != "$run_id" ]; then
        echo "$n already resumed"; continue
      fi
      msg='You were paused. Continue the same task from where you stopped: check git status/diff, finish the remaining steps and checks, then give the final report in the requested format.'
      if [ -n "$dir" ] && (cd "$dir" && wk "$n" -r "$msg" </dev/null) >/dev/null 2>&1; then
        echo "resumed $n"
      else
        echo "FAILED to resume $n" >&2
      fi
    done
    exit $? ;;
  wait) shift
    if [ $# -eq 0 ]; then  # no names: block until ANY running worker exits, name it
      before=$("$0" 2>/dev/null | awk '$2=="running" || $2=="STALLED"{print $1}')
      [ -n "$before" ] || { echo "no running workers"; exit 0; }
      while sleep 20; do
        now=$("$0" 2>/dev/null | awk '$2=="running" || $2=="STALLED"{print $1}')
        gone=$(printf '%s\n' "$before" | grep -vxF "$(printf '%s\n' "$now")")
        [ -n "$now" ] || gone=$before
        [ -n "$gone" ] && { printf '%s finished\n' $gone; exit 0; }
      done
    fi
    for n in "$@"; do
      ( r=$(row "$n"); [ -n "$r" ] || { echo "$n unknown"; exit; }
        pid=${r%%"$(printf '\t')"*}; l=${r#*"$(printf '\t')"}
        run_id=$(sed -n 's/^run_id=//p' "$l.run" 2>/dev/null)
        while [ "$(sed -n 's/^run_id=//p' "$l.run" 2>/dev/null)" = "$run_id" ] && ops_running "$l" "$pid"; do sleep 10; done
        tail -5 "$l" 2>/dev/null | grep -q '^ERROR:' && echo "$n ERRORED -> wk $n -r" || echo "$n finished" ) &
    done; wait; exit 0 ;;
  report) shift
    for n in "$@"; do r=$(row "$n"); [ -n "$r" ] && sh "$(dirname "$(readlink -f "$0")")/report.sh" "${r#*"$(printf '\t')"}" || echo "$n unknown"; done
    exit 0 ;;
  overlap)
    command -v wt-dev >/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    base=$(git rev-parse --abbrev-ref HEAD); tmp=$(mktemp -d); trap 'ctrash "$tmp" >/dev/null' EXIT
    wt-dev ls 2>/dev/null | while read -r name _; do
      path=$(wt-dev path "$name" 2>/dev/null) || continue
      { git -C "$path" diff --name-only "$base...HEAD" 2>/dev/null
        git -C "$path" status --porcelain=v1 -uall 2>/dev/null | cut -c4- | sed 's/.* -> //'
      } | grep -v -e '^\.reviews/' -e '^tools/file-size-baseline\.json$' | sort -u >"$tmp/$name"
    done
    set -- "$tmp"/*; [ -e "$1" ] || exit 0
    for a in "$@"; do for b in "$@"; do [ "$a" \< "$b" ] || continue
      common=$(comm -12 "$a" "$b"); [ -n "$common" ] || continue
      n=$(printf '%s\n' "$common" | wc -l | tr -d ' ')
      echo "OVERLAP ${a##*/} <-> ${b##*/}: $n file(s): $(printf '%s\n' "$common" | head -3 | tr '\n' ' ')"
    done; done
    exit 0 ;;
esac
all=0; [ "${1:-}" = "--all" ] && all=1
now=$(date +%s); stall=${BATCHES_STALL_MIN:-12}
flag=$(mktemp); done_n=$(mktemp); trap 'ctrash "$flag" "$done_n" >/dev/null' EXIT
age() { m=$(( (now - $1) / 60 )); [ $m -lt 60 ] && echo "${m}m" || echo "$((m / 60))h$((m % 60))m"; }
alive() { ops_running "$2" "$1"; }
is_acked() { grep -qx "$1" "$acked"; }
# Why a worker stopped: last error-looking line of its log (secret-looking lines skipped), ~80 chars.
cause() { m=$(tail -200 "$1" 2>/dev/null | grep -E "${2:-ERROR|[Ee]rror:|panic|Killed|capacity|usage limit|rate limit|exit code|stream disconnected}")
  [ -n "$m" ] || { echo "no error in log"; return; }
  c=$(printf '%s\n' "$m" | grep -viE 'token|secret|password|api[_-]?key|authorization' | tail -1 | tr -d '\033\r' |
  sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z? *//; s/^ +//' | tr -s ' ' | cut -c1-80)
  echo "${c:-error line withheld (looks like a secret)}"; }

[ -s "$reg" ] && awk -F'\t' -v n="$now" 'n-$1<=86400 {last[$3]=$0} END {for (k in last) print last[k]}' "$reg" |
  sort -n | while IFS="$(printf '\t')" read -r start pid log dir; do
    name=$(basename "$log" .log)
    mt=$(stat -f %m "$log" 2>/dev/null || echo "$start")
    if is_paused "$name" "$log"; then st="paused (batches resume $name)"
    elif [ -f "$log.exit" ]; then
      rc=$(sed -n 's/^rc=\([^ ]*\) ended=.*/\1/p' "$log.exit")
      if ops_reported "$log"; then
        echo x >>"$done_n"; [ $all -eq 1 ] || continue; st="done"
      elif is_acked "$name"; then continue
      elif [ "${rc:-0}" -lt 128 ] 2>/dev/null && tail -5 "$log" 2>/dev/null | grep -q '^ERROR:'; then
        st="ERRORED ($(tail -5 "$log" | cause /dev/stdin '^ERROR:' | sed 's/^ERROR: //')) -> wk $name -r"; echo 1 >"$flag"
      else st="DIED (rc=${rc:-unknown}$([ -s "$log.last" ] || printf ', no report')): $(cause "$log")"; echo 1 >"$flag"; fi
    elif alive "$pid" "$log"; then
      if [ $((now - mt)) -gt $((stall * 60)) ]; then
        is_acked "$name" && [ $all -eq 0 ] && continue
        st="STALLED log quiet $(age "$mt")"; is_acked "$name" || echo 1 >"$flag"
      else st="running $(age "$start")"; fi
    elif [ -f "$log.run" ]; then
      is_acked "$name" && continue
      st="DIED (no exit record; killed hard): $(cause "$log")"; echo 1 >"$flag"
    elif tail -5 "$log" 2>/dev/null | grep -q '^ERROR:' && ! is_acked "$name"; then
      st="ERRORED ($(tail -5 "$log" | cause /dev/stdin '^ERROR:' | sed 's/^ERROR: //')) -> wk $name -r"; echo 1 >"$flag"
    elif ops_reported "$log"; then
      echo x >>"$done_n"; [ $all -eq 1 ] || continue; st="done"
    elif is_acked "$name"; then continue
    else st="DIED (no report): $(cause "$log")"; echo 1 >"$flag"; fi
    case "$dir" in */worktrees/*) st="$st  @${dir##*/}" ;; esac
    printf '%-26s %s\n' "$name" "$st"
  done

# Pipelines without .done whose stages are all gone died mid-run.
[ -s "$reg" ] && awk -F'\t' -v n="$now" 'n-$1<=86400 {print $3}' "$reg" |
  sed -nE 's/\.(build|review|fix|fix2)\.log$//p' | sort -u | while read -r out; do
    [ -f "$out.done" ] && continue
    name=$(basename "$out"); is_acked "$name" && continue
    up=0
    for s in build review fix fix2; do p=$(cat "$out.$s.log.pid" 2>/dev/null) && alive "$p" "$out.$s.log" && up=1; done
    pgrep -f "pipeline.sh .*$out" >/dev/null && up=1
    [ $up -eq 0 ] && { printf '%-26s pipeline DIED before .done\n' "$name"; echo 1 >"$flag"; }
  done
[ -s "$done_n" ] && [ $all -eq 0 ] && echo "($(wc -l <"$done_n" | tr -d ' ') finished workers; --all lists them)"

if git rev-parse --git-dir >/dev/null 2>&1 && command -v wt-dev >/dev/null; then
  base=$(git rev-parse --abbrev-ref HEAD)
  # Worktree dirs that still host a live worker are busy, never "remove?".
  busy=$(mktemp); trap 'ctrash "$flag" "$done_n" "$busy" >/dev/null' EXIT
  [ -s "$reg" ] && awk -F'\t' '{last[$3]=$0} END{for (l in last) print last[l]}' "$reg" | while IFS="$(printf '\t')" read -r at pid log dir; do
    alive "$pid" "$log" && echo "$dir"; done >"$busy"
  wt-dev ls 2>/dev/null | while read -r name _ url dirty _rest; do
    path=$(wt-dev path "$name" 2>/dev/null) || continue
    ahead=$(git -C "$path" rev-list --count "$base..HEAD" 2>/dev/null || echo 0)
    d=${dirty#dirty:}; note=""
    [ "$ahead" -gt 0 ] && note="$ahead to land"
    [ "${d:-0}" -gt 0 ] && note="${note:+$note, }$d uncommitted"
    [ -z "$note" ] && grep -qxF "$path" "$busy" && note="worker running"
    printf 'wt %-23s %s %s\n' "$name" "${note:-empty, remove?}" "${url#http://localhost}"
  done
fi
[ -s "$flag" ] && exit 3; exit 0
