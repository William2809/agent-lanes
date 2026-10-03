#!/bin/sh
# ABOUTME: Short tracker of what needs the lead: workers running / DIED / STALLED, dead pipelines,
# ABOUTME: and wt-dev worktrees with work not yet merged. Finished items collapse to one count.
# Usage: batches            only what needs attention (cheap to run often)
#        batches --all      every worker of the last 24h
#        batches pause [NAME…]  pause named workers (no names: all running)
#        batches resume [NAME…] resume named workers (no names: all paused)
#        batches ack NAME…  mark a DIED worker/pipeline as handled (hidden afterwards)
#        batches wait [NAME…] block until those workers exit (no names: until any running one exits), one "NAME finished" line each
#                           (run as one background command for a notification)
#        batches report NAME…  final report of finished workers (report.sh on their logs)
#        batches overlap   unlanded worktrees that touch the same files (future land conflicts)
# Exit 3 when something needs the lead (DIED, STALLED). Run inside the repo for worktrees.
state="$HOME/.claude/state"; reg="$state/workers.tsv"; acked="$state/acked"; paused="$state/paused-workers.tsv"
mkdir -p "$state"; touch "$acked"
# Import the old dock queue once; keep the source in trash.
if [ -s "$state/dock/paused.txt" ]; then
  cat "$state/dock/paused.txt" >>"$paused" && ctrash "$state/dock/paused.txt" >/dev/null || exit 1
fi
is_paused() { awk -F'\t' -v n="$1" '$1==n {found=1} END {exit !found}' "$paused" 2>/dev/null; }
if [ "${1:-}" = "ack" ]; then shift; for n in "$@"; do echo "$n" >>"$acked"; done; exit 0; fi
# Latest registry row for a worker name (log basename without .log): "pid<TAB>log".
row() { awk -F'\t' -v n="$1" '{b=$3; sub(/.*\//,"",b); sub(/\.log$/,"",b)} b==n {r=$2"\t"$3} END {if (r) print r}' "$reg"; }
case "${1:-}" in
  pause) shift
    [ -s "$reg" ] || exit 0
    # Only workers the status listing calls running: an old registry pid may now belong to another process.
    live=$("$0" 2>/dev/null | awk '$2=="running"{print $1}')
    [ $# -eq 0 ] && set -- $live
    for n in "$@"; do
      printf '%s\n' "$live" | grep -qx "$n" || { echo "$n is not running" >&2; continue; }
      is_paused "$n" && continue
      r=$(row "$n"); [ -n "$r" ] || continue
      pid=${r%%"$(printf '\t')"*}
      kill -0 "$pid" 2>/dev/null || continue
      log=${r#*"$(printf '\t')"}
      dir=$(awk -F'\t' -v target="$log" '$3==target {d=$4} END {print d}' "$reg")
      if kill "$pid" 2>/dev/null; then
        printf '%s\t%s\n' "$n" "$dir" >>"$paused"
        echo "paused $n"
      fi
    done
    exit 0 ;;
  resume) shift
    [ -s "$paused" ] || { echo "no paused workers"; exit 0; }
    cp "$paused" "$paused.$(date +%Y%m%d-%H%M%S).$$" || exit 1
    left="$paused.new"; : >"$left" || exit 1
    sort -u "$paused" | while IFS="$(printf '\t')" read -r n dir; do
      selected=0; [ $# -eq 0 ] && selected=1
      for wanted in "$@"; do [ "$wanted" = "$n" ] && selected=1; done
      if [ "$selected" -eq 0 ]; then printf '%s\t%s\n' "$n" "$dir" >>"$left"; continue; fi
      msg='You were paused. Continue the same task from where you stopped: check git status/diff, finish the remaining steps and checks, then give the final report in the requested format.'
      if [ -n "$dir" ] && (cd "$dir" && wk "$n" -r "$msg" </dev/null) >/dev/null 2>&1; then
        echo "resumed $n"
      else
        echo "FAILED to resume $n" >&2
        printf '%s\t%s\n' "$n" "$dir" >>"$left"
      fi
    done
    mv "$left" "$paused"
    exit $? ;;
  wait) shift
    if [ $# -eq 0 ]; then  # no names: block until ANY running worker exits, name it
      before=$("$0" 2>/dev/null | awk '$2=="running"{print $1}')
      [ -n "$before" ] || { echo "no running workers"; exit 0; }
      while sleep 20; do
        now=$("$0" 2>/dev/null | awk '$2=="running"{print $1}')
        gone=$(printf '%s\n' "$before" | grep -vxF "$(printf '%s\n' "$now")")
        [ -n "$now" ] || gone=$before
        [ -n "$gone" ] && { printf '%s finished\n' $gone; exit 0; }
      done
    fi
    for n in "$@"; do
      ( r=$(row "$n"); [ -n "$r" ] || { echo "$n unknown"; exit; }
        pid=${r%%"$(printf '\t')"*}; while kill -0 "$pid" 2>/dev/null; do sleep 10; done
        l=${r#*"$(printf '\t')"}; tail -5 "$l" 2>/dev/null | grep -q '^ERROR:' && echo "$n ERRORED -> wk $n -r" || echo "$n finished" ) &
    done; wait; exit 0 ;;
  report) shift
    for n in "$@"; do r=$(row "$n"); [ -n "$r" ] && sh "$(dirname "$(readlink -f "$0")")/report.sh" "${r#*"$(printf '\t')"}" || echo "$n unknown"; done
    exit 0 ;;
  overlap)
    command -v wt-dev >/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    base=$(git rev-parse --abbrev-ref HEAD); tmp=$(mktemp -d); trap 'rm -r "$tmp"' EXIT
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
flag=$(mktemp); done_n=$(mktemp); trap 'rm -f "$flag" "$done_n"' EXIT
age() { m=$(( (now - $1) / 60 )); [ $m -lt 60 ] && echo "${m}m" || echo "$((m / 60))h$((m % 60))m"; }
# A pid counts only while it is still a codex process (pids get reused).
# ps can be blind inside a sandboxed shell, so also trust kill -0 and a log written in the last 3 minutes.
alive() { ps -p "$1" -o command= 2>/dev/null | grep -q codex || kill -0 "$1" 2>/dev/null \
  || { [ -n "${2:-}" ] && ! grep -q '^tokens used' "$2" && [ $((now - $(stat -f %m "$2" 2>/dev/null || echo 0))) -lt 180 ]; }; }
is_acked() { grep -qx "$1" "$acked"; }

[ -s "$reg" ] && awk -F'\t' -v n="$now" 'n-$1<=86400 {last[$3]=$0} END {for (k in last) print last[k]}' "$reg" |
  sort -n | while IFS="$(printf '\t')" read -r start pid log dir; do
    name=$(basename "$log" .log)
    mt=$(stat -f %m "$log" 2>/dev/null || echo "$start")
    if is_paused "$name"; then st="paused (batches resume $name)"
    elif alive "$pid" "$log"; then
      if [ $((now - mt)) -gt $((stall * 60)) ]; then st="STALLED log quiet $(age "$mt")"; echo 1 >"$flag"
      else st="running $(age "$start")"; fi
    elif tail -5 "$log" 2>/dev/null | grep -q '^ERROR:' && ! is_acked "$name"; then
      st="ERRORED ($(tail -5 "$log" | grep '^ERROR:' | tail -1 | cut -c8-60)) -> wk $name -r"; echo 1 >"$flag"
    elif grep -q '^tokens used' "$log" 2>/dev/null; then
      echo x >>"$done_n"; [ $all -eq 1 ] || continue; st="done"
    elif is_acked "$name"; then continue
    else st="DIED (no report)"; echo 1 >"$flag"; fi
    case "$dir" in */worktrees/*) st="$st  @${dir##*/}" ;; esac
    printf '%-26s %s\n' "$name" "$st"
  done

# Pipelines without .done whose stages are all gone died mid-run.
[ -s "$reg" ] && awk -F'\t' -v n="$now" 'n-$1<=86400 {print $3}' "$reg" |
  sed -nE 's/\.(build|review|fix|fix2)\.log$//p' | sort -u | while read -r out; do
    [ -f "$out.done" ] && continue
    name=$(basename "$out"); is_acked "$name" && continue
    up=0
    for s in build review fix fix2; do p=$(cat "$out.$s.log.pid" 2>/dev/null) && alive "$p" && up=1; done
    pgrep -f "pipeline.sh .*$out" >/dev/null && up=1
    [ $up -eq 0 ] && { printf '%-26s pipeline DIED before .done\n' "$name"; echo 1 >"$flag"; }
  done
[ -s "$done_n" ] && [ $all -eq 0 ] && echo "($(wc -l <"$done_n" | tr -d ' ') finished workers; --all lists them)"

if git rev-parse --git-dir >/dev/null 2>&1 && command -v wt-dev >/dev/null; then
  base=$(git rev-parse --abbrev-ref HEAD)
  # Worktree dirs that still host a live worker are busy, never "remove?".
  busy=$(mktemp); trap 'rm -f "$flag" "$done_n" "$busy"' EXIT
  [ -s "$reg" ] && awk -F'\t' '{print $2"\t"$4}' "$reg" | while IFS="$(printf '\t')" read -r pid dir; do
    alive "$pid" && echo "$dir"; done >"$busy"
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
