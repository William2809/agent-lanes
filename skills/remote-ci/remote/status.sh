#!/bin/bash
# ABOUTME: Prints remote-ci state for one project: route, lock, Postgres, last runs, disk.
. "$HOME/ci/bin/lib.sh"
load_project "$1" || exit $?
REMOTE_CI_SLOTS=${3:-$REMOTE_CI_SLOTS}
# Also display live slots from another worktree using a larger slot setting.
for f in "$dir"/slots/*.lock; do
  lock_live "$f" || continue
  number=$(basename "$f" .lock)
  [ "$number" -gt "$REMOTE_CI_SLOTS" ] && REMOTE_CI_SLOTS=$number
done
echo "route: $2"
if [ -f "$ci/lock" ]; then echo "lock: held by pid $(cat "$ci/lock")"; else echo "lock: free"; fi
echo "slots: $REMOTE_CI_SLOTS"
for ((slot=1; slot<=REMOTE_CI_SLOTS; slot++)); do
  if [ "$REMOTE_CI_SLOTS" = 1 ]; then f=$ci/lock; else f=$dir/slots/$slot.lock; fi
  if lock_live "$f"; then
    echo "  slot $slot: busy pid=$(cat "$f") run=$(cat "$dir/slots/$slot.run" 2>/dev/null || echo exclusive)"
  elif lock_live "$ci/lock"; then
    echo "  slot $slot: unavailable (exclusive runner/setup/prune or scheduler guard pid=$(cat "$ci/lock"))"
  else echo "  slot $slot: idle"; fi
done
if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
  pg_ctl -D "$pgdata" status >/dev/null 2>&1 && echo "postgres $REMOTE_CI_POSTGRES: running" || echo "postgres $REMOTE_CI_POSTGRES: stopped"
fi
echo "last runs:"
ls -t "$dir"/results/*.exit 2>/dev/null | head -5 | while read -r f; do echo "  $(basename "$f" .exit) exit=$(cat "$f")"; done
echo "disk free: $(df -h / | tail -1 | awk '{print $4}'), ~/ci: $(du -sh "$ci" | cut -f1)"
