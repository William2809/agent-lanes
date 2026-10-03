#!/bin/bash
# ABOUTME: Daily remote-ci cleanup (launchd 04:00) for every project: leftover test databases, caches, logs.
# ABOUTME: Skips while any run holds the lock; leaves Postgres stopped.
set -u
. "$HOME/ci/bin/lib.sh"
mkdir -p "$ci/logs"
[ -f "$ci/logs/prune.log" ] && tail -c 200000 "$ci/logs/prune.log" >"$ci/logs/prune.tmp" && mv "$ci/logs/prune.tmp" "$ci/logs/prune.log"
exec >>"$ci/logs/prune.log" 2>&1
echo "== prune $(date '+%F %T')"
(! lock_live "$ci/lock" && /usr/bin/shlock -f "$ci/lock" -p $$) || { echo "run in progress; skipped"; exit 0; }
trap 'rm -f "$ci/lock"' EXIT
slots_active && { echo "slot run in progress; skipped"; exit 0; }
for conf in "$ci"/projects/*/conf; do
  [ -f "$conf" ] || continue
  project=$(basename "$(dirname "$conf")")
  ( load_project "$project"
    if [ -n "${REMOTE_CI_POSTGRES:-}" ] && [ -n "${REMOTE_CI_DB_CLEAN:-}" ] && [ -f "$pgdata/PG_VERSION" ]; then
      (pg_ctl -D "$pgdata" status >/dev/null 2>&1 || pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w >/dev/null) &&
        psql -d postgres -Atqc "select datname from pg_database where datname like '$REMOTE_CI_DB_CLEAN'" |
        while read -r db; do dropdb --if-exists "$db"; done
      pg_ctl -D "$pgdata" stop -m fast >/dev/null 2>&1
    fi
    command -v pnpm >/dev/null && (cd "$dir/work" && pnpm store prune >/dev/null 2>&1)
    ls -t "$dir"/logs/*.log 2>/dev/null | tail -n +21 | xargs rm -f
    ls -t "$dir"/results/*.exit 2>/dev/null | tail -n +41 | xargs rm -f
    ls -t "$dir"/results/tree-*.pass 2>/dev/null | tail -n +41 | xargs rm -f
    for work in "$dir/work" "$dir"/work-slot*; do
      [ -d "$work" ] || continue
      kb=$(du -sk "$work/.turbo" 2>/dev/null | cut -f1)
      [ "${kb:-0}" -gt 2000000 ] && rm -rf "$work/.turbo"
    done
    # Refs of runs that never finished (no run holds the lock now).
    git -C "$dir/repo.git" for-each-ref --format='%(refname)' refs/heads/run refs/heads/check |
      while read -r ref; do
        # Queued callers have a ref but no result yet; leave their submission intact.
        [ -f "$dir/results/${ref##*/}.exit" ] && git -C "$dir/repo.git" update-ref -d "$ref"
      done
    for submission in "$dir"/runs/*; do
      [ -d "$submission" ] || continue
      [ -f "$dir/results/${submission##*/}.exit" ] && rm -rf "$submission"
    done
    git -C "$dir/repo.git" gc --auto -q
    echo "$project ok" )
done
for f in "$ci"/pg/*.log; do [ -f "$f" ] && tail -c 500000 "$f" >"$f.tmp" && mv "$f.tmp" "$f"; done
echo "disk free: $(df -h / | tail -1 | awk '{print $4}'); ~/ci: $(du -sh "$ci" | cut -f1)"
