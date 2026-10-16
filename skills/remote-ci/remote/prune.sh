#!/bin/bash
# ABOUTME: Daily remote-ci cleanup (launchd 04:00) for every project: leftover test databases, caches, logs.
# ABOUTME: Skips while any run holds the lock; leaves Postgres stopped.
set -u
# One runner deletion entry point. Accept managed cache entries and install outputs
# only. Refuse symlinks in ancestors, traversal, and broad project/cluster roots.
if [ "${1:-}" = --cache-delete ]; then
  python3 - "$HOME/ci" "${2:-}" <<'CHECK'
from pathlib import Path
import sys
logical = Path(sys.argv[1]).absolute()
root = logical.resolve()
supplied = Path(sys.argv[2]).absolute()
try:
    p = root / supplied.relative_to(logical)
except ValueError:
    p = supplied
if '..' in p.parts or p == root or root not in p.parent.resolve().parents and p.parent.resolve() != root:
    sys.exit(2)
rel = p.relative_to(root).parts
cache = len(rel) >= 2 and rel[0] == 'cache' and rel[1] != 'registry.json' and rel[1] != 'registry.lock'
output = len(rel) >= 4 and rel[0] == 'projects' and (rel[2] == 'work' or rel[2].startswith('work-slot')) and rel[-1] in ('node_modules', '.venv', '.turbo')
template = len(rel) == 4 and rel[0] == 'projects' and rel[2] == 'templates' and rel[3].endswith('.ready')
disposable = len(rel) == 2 and rel[0] == 'pg' and rel[1].endswith(('-disposable', '-minimal'))
run = len(rel) == 4 and rel[0] == 'projects' and rel[2] == 'runs' and (root / 'projects' / rel[1] / 'results' / (rel[3] + '.exit')).is_file()
if not (cache or output or template or disposable or run):
    sys.exit(2)
if p.parent.resolve() != p.parent:
    sys.exit(2)
CHECK
  [ $? = 0 ] || { echo 'cache delete refused' >&2; exit 2; }
  [ ! -e "$2" ] && [ ! -L "$2" ] && exit 0
  # chmod never follows image symlinks. Removal stays inside this prune mechanism.
  python3 - "$2" <<'MODES'
import os, stat, sys
if os.path.islink(sys.argv[1]):
    sys.exit(0)
for base, dirs, files in os.walk(sys.argv[1], followlinks=False):
    for p in [base] + [os.path.join(base, n) for n in dirs + files]:
        if not os.path.islink(p):
            os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | stat.S_IWUSR)
MODES
  rm -rf -- "$2"
  exit $?
fi
runner_dir=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" = --admitted-v2 ]; then
  python3 "$runner_dir/resources.py" verify --token "${AGENT_CI_TOKEN:-}" \
    --generation "${AGENT_CI_LEASE_GENERATION:-0}" --owner "$$" --phase maintenance || exit 74
fi
scheduler_version=$(python3 "$runner_dir/resources.py" scheduler-version) || exit 74
if [ "$scheduler_version" = v2 ] && [ "${1:-}" != --admitted-v2 ]; then
  exec python3 "$runner_dir/scheduler_maintenance.py" prune
fi
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"
prune_dir=$runner_dir
mkdir -p "$ci/logs"
[ -f "$ci/logs/prune.log" ] && tail -c 200000 "$ci/logs/prune.log" >"$ci/logs/prune.tmp" && mv "$ci/logs/prune.tmp" "$ci/logs/prune.log"
exec >>"$ci/logs/prune.log" 2>&1
echo "== prune $(date '+%F %T')"
(! lock_live "$ci/lock" && platform_lock "$ci/lock" $$) || { echo "run in progress; skipped"; exit 0; }
trap 'rm -f "$ci/lock"' EXIT
slots_active && { echo "slot run in progress; skipped"; exit 0; }
prune_failed=0 prune_v2=0
[ "${1:-}" != --admitted-v2 ] || prune_v2=1
quarantined=""
if [ "$prune_v2" = 1 ]; then
  quarantined=$(python3 "$runner_dir/resources.py" quarantined-projects) || exit 76
fi
# V2 quarantine can lack a legacy .cleanup file; retain its cache pins.
[ -n "$quarantined" ] || python3 "$prune_dir/cache.py" reap-idle || exit 1
for conf in "$ci"/projects/*/conf; do
  [ -f "$conf" ] || continue
  project=$(basename "$(dirname "$conf")")
  case " $quarantined " in
    *" $project "*) echo "$project quarantine pending; skipped"; continue ;;
  esac
  ( load_project "$project"
    for pending in "$dir"/results/*.cleanup; do
      [ ! -f "$pending" ] || { echo "$project cleanup pending; skipped"; exit 0; }
    done
    pg_profile_ensure || exit 1
    if [ -n "${REMOTE_CI_POSTGRES:-}" ] && [ -f "$pgdata/PG_VERSION" ] &&
      { pg_ctl -D "$pgdata" status >/dev/null 2>&1 || pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w >/dev/null; }; then
      [ -z "${REMOTE_CI_DB_CLEAN:-}" ] ||
        psql -d postgres -Atqc "select datname from pg_database where datname like '$REMOTE_CI_DB_CLEAN'" |
        while read -r db; do dropdb --if-exists "$db" || { [ "$prune_v2" = 0 ] || exit 76; }; done
      db_status="${PIPESTATUS[*]}"
      [ "$prune_v2" = 0 ] || [ "$db_status" = "0" ] || [ "$db_status" = "0 0" ] || exit 76
      # Template cache: keep the 5 newest and any used in 3 days. run.sh rebuilds a missing one.
      prefix=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_
      keep=$({ ls -t "$dir"/templates/*.ready 2>/dev/null | head -5; find "$dir/templates" -name '*.ready' -mtime -3 2>/dev/null; } | sed 's|.*/||; s|\.ready$||')
      psql -d postgres -Atqc "select datname from pg_database where datname ~ '^${prefix}[0-9a-f]{32}\$'" |
        while read -r db; do
          python3 "$prune_dir/cache.py" contains --key "$db" && continue
          printf '%s\n' "$keep" | grep -qxF "$db" && continue
          rm -f "$dir/templates/$db.ready"
          dropdb --if-exists --force "$db" && echo "$project template dropped: $db" || { [ "$prune_v2" = 0 ] || exit 76; }
        done
      db_status="${PIPESTATUS[*]}"
      [ "$prune_v2" = 0 ] || [ "$db_status" = "0 0" ] || exit 76
      # Whole-entry LRU includes database bytes and respects run pins.
      for marker in "$dir"/templates/*.ready; do
        [ -f "$marker" ] || continue
        db=$(basename "$marker" .ready)
        python3 "$prune_dir/cache.py" contains --key "$db" && continue
        case "$db" in mci_*) ;; *) continue ;; esac
        case "$db" in *[!a-z0-9_]*) continue ;; esac
        bytes=$(psql -d postgres -Atqc "select pg_database_size('$db')" 2>/dev/null) || continue
        case "$bytes" in ''|*[!0-9]*) continue ;; esac
        metadata=$(python3 -c 'import json,sys; print(json.dumps(dict(database=sys.argv[1],port=sys.argv[2],dropdb=sys.argv[3])))' "$db" "$pgport" "$pgbin/dropdb")
        python3 "$prune_dir/cache.py" register --key "$db" --kind template \
          --path "$marker" --size "$bytes" --metadata "$metadata" --used-from-path || exit 1
      done
      python3 "$prune_dir/cache.py" prune || true
      pg_ctl -D "$pgdata" stop -m fast >/dev/null 2>&1 || { [ "$prune_v2" = 0 ] || exit 76; }
    fi
    for submission in "$dir"/runs/*; do
      [ -d "$submission" ] || continue
      [ -f "$dir/results/${submission##*/}.exit" ] && bash "$prune_dir/prune.sh" --cache-delete "$submission"
    done
    ls -t "$dir"/logs/*.log 2>/dev/null | tail -n +21 | xargs rm -f
    ls -t "$dir"/results/*.exit 2>/dev/null | tail -n +41 |
      while IFS= read -r result; do
        base=${result%.exit}
        rm -f "$result" "$base.resource-stop" "$base.resource-stop.tree"
      done
    ls -t "$dir"/results/tree-*.pass 2>/dev/null | tail -n +41 | xargs rm -f
    # Refs of runs that never finished (no run holds the lock now).
    git -C "$dir/repo.git" for-each-ref --format='%(refname)' refs/heads/run refs/heads/check |
      while read -r ref; do
        # Queued callers have a ref but no result yet; leave their submission intact.
        [ -f "$dir/results/${ref##*/}.exit" ] && git -C "$dir/repo.git" update-ref -d "$ref"
      done
    git -C "$dir/repo.git" gc --auto -q
    echo "$project ok" ) || prune_failed=1
done
python3 "$prune_dir/cache.py" prune || true
for f in "$ci"/pg/*.log; do [ -f "$f" ] && tail -c 500000 "$f" >"$f.tmp" && mv "$f.tmp" "$f"; done
echo "disk free: $(df -h / | tail -1 | awk '{print $4}'); ~/ci: $(du -sh "$ci" | cut -f1)"
[ "$prune_v2" = 0 ] || [ "$prune_failed" = 0 ] || exit 76
