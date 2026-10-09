#!/bin/sh
# ABOUTME: Delivery receipts for worker reports (<record>.delivered), changed only under the record lock.
# Usage: deliver.sh claim|confirm|release SESSION LOG RUN_ID [LOG RUN_ID ...]
# A receipt is "RUN_ID" once a session queued the report, or "pending RUN_ID SESSION EPOCH" while
# one is sending it (state first: a cut-off pending write can never read as a final receipt). The workers mod claims before it sends and confirms after, so two sessions never
# both send a report, and a relaunch cannot archive the record between the lookup and the write.
#   claim:   prints "claimed RUN_ID" (send it), "taken RUN_ID" (already sent) or "busy RUN_ID" (another
#            session is sending; retry later). A pending receipt older than DELIVER_STALE seconds
#            (default 300: its sender crashed or timed out) is taken over, so no report is lost; a sender
#            that crashed after queueing but before confirming can make it arrive twice.
#   confirm: the report was queued: the receipt becomes final.
#   release: the prompt was not queued: this session's pending receipt is emptied.
# A run with no output line could not be read (retry later).
PATH="$HOME/.local/bin:$PATH"
. "$(cd "$(dirname "$0")" && pwd)/state.sh"
mode=${1:-}; by=${2:-}
case "$mode:$by" in claim:?*|confirm:?*|release:?*) shift 2 ;; *) echo "usage: deliver.sh claim|confirm|release SESSION LOG RUN_ID ..." >&2; exit 2 ;; esac
stale=${DELIVER_STALE:-300}
rc=0
# Receipts are written by this shell itself (builtin printf, no mv or ctrash): a helper killed while
# holding the lock leaves no child that could later change a newer attempt's receipt at the same path.
# Readers only ask whether a receipt is exactly its run ID, so a torn read means "not delivered".
write() { printf '%s\n' "$2" >"$1.delivered"; }
while [ $# -ge 2 ]; do
  log=$1 run=$2; shift 2
  ops_lock "$log.record.lock" || { rc=1; continue; }
  rec=""
  for r in "$log" "${log%.log}".*.log; do
    [ -f "$r.run" ] && [ "$(sed -n 's/^run_id=//p' "$r.run")" = "$run" ] && { rec=$r; break; }
  done
  if [ -z "$rec" ]; then rc=1
  else
    receipt=$(cat "$rec.delivered" 2>/dev/null) || receipt=""
    # $receipt fields: RUN_ID, or pending RUN_ID SESSION EPOCH
    read -r r_state r_run r_by r_at <<EOF
$receipt
EOF
    # An older helper wrote RUN_ID pending SESSION EPOCH: its sender may still be sending.
    [ "$r_run" != pending ] || { r_run=$r_state; r_state=pending; }
    # A malformed or future time counts as stale.
    now=$(date +%s); r_at=$(printf '%s' "$r_at" | sed 's/^0*//')
    case "$r_at" in ''|*[!0-9]*|??????????????*) r_at=0 ;; esac
    [ "$r_at" -le "$((now + 60))" ] || r_at=0
    ours=0; [ "$r_run" = "$run" ] && [ "$r_state" = pending ] && [ "$r_by" = "$by" ] && ours=1
    case $mode in
    claim)
      if [ "$receipt" = "$run" ]; then echo "taken $run"
      elif [ "$r_run" = "$run" ] && [ "$r_state" = pending ] && [ "$ours" = 0 ] &&
        [ "$((now - r_at))" -lt "$stale" ]; then echo "busy $run"
      elif write "$rec" "pending $run $by $now"; then echo "claimed $run"
      else rc=1
      fi ;;
    confirm) [ "$ours" = 0 ] || write "$rec" "$run" || rc=1 ;;
    release) [ "$ours" = 0 ] || : >"$rec.delivered" || rc=1 ;;
    esac
  fi
  ops_unlock "$log.record.lock" || rc=1
done
exit $rc
