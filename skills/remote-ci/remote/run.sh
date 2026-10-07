#!/bin/bash
# ABOUTME: Checks one exact commit in an isolated slot with optional install and database templates.
# ABOUTME: Coordinates shared Postgres and preserves the global single-run default for existing configs.
# Usage: run.sh <project> <commit> <tree> <run-id> [<base64 command>] [<force>]
set -u
runner_dir=$(cd "$(dirname "$0")" && pwd)
. "$runner_dir/lib.sh"
if [ -f "$runner_dir/../conf" ]; then
  load_project "$1" "$runner_dir/../conf" || { echo 2 >"$dir/results/$4.exit"; exit 2; }
  env_file=$runner_dir/../env.local
else
  load_project "$1" || { echo 2 >"$dir/results/$4.exit"; exit 2; }
  env_file=$dir/env.local
fi
sha=$2 tree=$3 run=$4
command=$(printf '%s' "${5:-}" | base64 -D 2>/dev/null)
force=${6:-0}
log=$dir/logs/$run.log
exec >"$log" 2>&1
submitted=$(date +%s)
child="" watchdog="" locked=0 slot=0 template_lock="" pg_started=0 guard_owned=0 template_locked=0
claim=$dir/results/tree-$tree.running
claim_owned=0 cache_result=0
work=$dir/work
base_db=$REMOTE_CI_DB
role=ci
finish() {
  local rc=${1:-1} cleanup_start owner_filter=""
  trap '' HUP INT TERM
  if [ -n "$watchdog" ]; then
    kill -TERM -"$watchdog" 2>/dev/null || kill -TERM "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
  fi
  if [ -n "$child" ]; then
    kill -TERM -"$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null
    sleep 1
    kill -KILL -"$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  [ -n "${child_marker:-}" ] && rm -f "$child_marker"
  [ "$template_locked" = 1 ] && rm -f "$template_lock.child"
  cleanup_start=$(date +%s)
  [ "$template_locked" = 1 ] && rm -f "$template_lock"
  # A queued/busy run has no resources and must never stop another run's Postgres.
  if [ "$locked" = 1 ] && [ "$pg_started" = 1 ]; then
    [ "$REMOTE_CI_SLOTS" -gt 1 ] && owner_filter="and datdba = (select oid from pg_roles where rolname = '$role')"
    if [ -n "${REMOTE_CI_DB_CLEAN:-}" ]; then
      psql -d postgres -Atq -v pattern="$REMOTE_CI_DB_CLEAN" -v role="$role" <<SQL |
select datname from pg_database
where datname like :'pattern' $owner_filter;
SQL
        while read -r db; do dropdb --if-exists --force "$db" || exit 1; done
      [ "${PIPESTATUS[*]}" = "0 0" ] || { echo "Error: database cleanup failed"; rc=1; }
    fi
    if [ "$REMOTE_CI_SLOTS" -gt 1 ]; then dropdb --if-exists --force "$REMOTE_CI_DB" || rc=1; fi
  fi
  if [ "$locked" = 1 ]; then
    if [ "$REMOTE_CI_SLOTS" -gt 1 ]; then
      acquire_guard
      rm -f "$dir/slots/$slot.lock" "$dir/slots/$slot.run"
    fi
    stop_idle_postgres
    release_guard
  fi
  git -C "$dir/repo.git" update-ref -d "refs/heads/run/$run" 2>/dev/null
  if [ "$cache_result" = 1 ]; then
    if [ "$rc" = 0 ]; then echo "$sha" >"$dir/results/tree-$tree.pass"
    else rm -f "$dir/results/tree-$tree.pass"; fi
  fi
  echo "== timing cleanup $(( $(date +%s) - cleanup_start ))s"
  echo "== timing total $(( $(date +%s) - submitted ))s (including queue)"
  echo "== finished rc=$rc $(date '+%F %T')"
  # Publish completion atomically after cleanup, logging and slot release.
  echo "$rc" >"$dir/results/$run.exit.new"
  mv "$dir/results/$run.exit.new" "$dir/results/$run.exit"
  if [ "$claim_owned" = 1 ]; then
    rm -f "$claim.run" "$claim"
    claim_owned=0
  fi
}
trap 'finish 1; exit 1' HUP INT TERM
run_live() {
  case "$1" in *[!0-9]* | "" | 0) return 1 ;; esac
  kill -0 "$1" 2>/dev/null && ps -p "$1" -o command= 2>/dev/null | grep -q 'run.sh'
}
busy_claim() { echo "busy: $*"; finish 75; exit 75; }
# shlock publishes the PID atomically. The sidecar binds that PID to a run ID;
# readers wait for matching metadata rather than following a previous owner.
if [ -z "$command" ] && [ "$force" = 0 ]; then
  while :; do
    owner_pid="" followed=""
    read -r owner_pid followed 2>/dev/null <"$claim.run" || true
    if [ -n "$followed" ] && ! run_live "$owner_pid" && run_child_live "$followed" "$owner_pid"; then
      busy_claim "run $followed still has a live slot child (log $dir/logs/$followed.log)"
    fi
    if /usr/bin/shlock -f "$claim" -p $$; then
      # The owner may die between inspection and shlock. Preserve its metadata
      # if a child still runs, so the next retry can make the same check.
      owner_pid="" followed=""
      read -r owner_pid followed 2>/dev/null <"$claim.run" || true
      if [ -n "$followed" ] && run_child_live "$followed" "$owner_pid"; then
        rm -f "$claim"
        busy_claim "run $followed still has a live slot child (log $dir/logs/$followed.log)"
      fi
      break
    fi
    [ $(( $(date +%s) - submitted )) -lt 300 ] || busy_claim "tree claim metadata unavailable after 300 seconds"
    owner=$(cat "$claim" 2>/dev/null)
    case "$owner" in *[!0-9]* | "" | 0) sleep 1; continue ;; esac
    if kill -0 "$owner" 2>/dev/null && ! run_live "$owner"; then
      # The pid was reused by another program: the claim is stale, so take it over.
      echo "== stale tree claim (pid $owner is not run.sh); replacing it"
      rm -f "$claim" "$claim.run"; continue
    fi
    if [ "$owner" = "$owner_pid" ] && [ -n "$followed" ] && run_live "$owner"; then
      echo "== following run $followed sha $sha (log $dir/logs/$followed.log)"
      follow_started=$(date +%s)
      while [ ! -f "$dir/results/$followed.exit" ]; do
        [ $(( $(date +%s) - follow_started )) -lt "$REMOTE_CI_TIMEOUT" ] ||
          busy_claim "followed run $followed exceeded $REMOTE_CI_TIMEOUT seconds (log $dir/logs/$followed.log)"
        if ! run_live "$owner"; then
          # Recheck publication after death: finish writes the exit before releasing its claim.
          [ -f "$dir/results/$followed.exit" ] && break
          echo "Error: followed run ended without a result (log $dir/logs/$followed.log)"
          finish 1; exit 1
        fi
        sleep 1
      done
      rc=$(cat "$dir/results/$followed.exit")
      [ "$rc" = 0 ] || echo "Error: followed run rc=$rc (log $dir/logs/$followed.log)"
      finish "$rc"; exit "$rc"
    fi
    sleep 1
  done
  claim_owned=1
  printf '%s %s\n' "$$" "$run" >"$claim.run.new"
  mv "$claim.run.new" "$claim.run"
fi
mkdir -p "$dir/slots"
while [ "$locked" = 0 ]; do
  if ! lock_live "$ci/lock" && /usr/bin/shlock -f "$ci/lock" -p $$; then
    guard_owned=1
    if [ "$REMOTE_CI_SLOTS" = 1 ]; then
      if ! slots_active; then locked=1; slot=1; fi
    else
      for ((candidate=1; candidate<=REMOTE_CI_SLOTS; candidate++)); do
        if ! lock_live "$dir/slots/$candidate.lock" && /usr/bin/shlock -f "$dir/slots/$candidate.lock" -p $$; then
          slot=$candidate; locked=1
          printf '%s\n' "$run" >"$dir/slots/$slot.run"
          work=$dir/work-slot$slot
          REMOTE_CI_DB=${base_db}_s$slot
          if [ "${#REMOTE_CI_DB}" -gt 63 ]; then
            REMOTE_CI_DB=mci_$(printf '%s' "$base_db" | shasum -a 256 | cut -c1-24)_s$slot
          fi
          role=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_s$slot
          break
        fi
      done
    fi
    if [ "$REMOTE_CI_SLOTS" -gt 1 ] || [ "$locked" = 0 ]; then release_guard; fi
  fi
  [ "$locked" = 1 ] && break
  if [ $(( $(date +%s) - submitted )) -ge 300 ]; then
    echo "busy: all $REMOTE_CI_SLOTS slots unavailable (or exclusive runner/prune active)"
    finish 75; exit 75
  fi
  sleep 2
done
if [ -z "$command" ] && [ "$force" = 0 ] && [ -f "$dir/results/tree-$tree.pass" ]; then
  echo "== cached: identical tree passed while queued"
  finish 0; exit 0
fi
[ -z "$command" ] && cache_result=1
started=$(date +%s)
echo "== $project run $run sha $sha slot $slot/$REMOTE_CI_SLOTS start $(date '+%F %T')"
echo "== timing queue $((started - submitted))s"
step() { echo "== $*"; "$@" || { finish 1; exit 1; }; }
phase() {
  local label=$1 start rc; shift
  start=$(date +%s)
  "$@"; rc=$?
  echo "== timing $label $(( $(date +%s) - start ))s"
  [ "$rc" = 0 ] || { finish "$rc"; exit "$rc"; }
}
supervised() {
  local rc
  perl -e 'setpgrp(0,0); exec @ARGV' "$@" &
  child=$!
  if [ "$REMOTE_CI_SLOTS" -gt 1 ]; then child_marker=$dir/slots/$slot.lock.child; else child_marker=$ci/lock.child; fi
  printf '%s\n' "$child" >"$child_marker"
  [ "$template_locked" = 1 ] && printf '%s\n' "$child" >"$template_lock.child"
  perl -e 'setpgrp(0,0); exec @ARGV' /bin/bash -c '
    sleep "$1"; echo "timeout after $1 seconds"
    kill -TERM -"$2" 2>/dev/null; sleep 5; kill -KILL -"$2" 2>/dev/null
  ' _ "$REMOTE_CI_TIMEOUT" "$child" &
  watchdog=$!
  wait "$child"; rc=$?
  # Reap surviving descendants before slot reuse, including on a failed command.
  kill -TERM -"$child" 2>/dev/null || true
  if kill -0 -"$child" 2>/dev/null; then sleep 1; kill -KILL -"$child" 2>/dev/null || true; fi
  rm -f "$child_marker"
  [ "$template_locked" = 1 ] && rm -f "$template_lock.child"
  child=""
  kill -TERM -"$watchdog" 2>/dev/null || kill -TERM "$watchdog" 2>/dev/null
  wait "$watchdog" 2>/dev/null; watchdog=""
  return "$rc"
}
checkout() {
  [ -d "$work/.git" ] || git init -q "$work" || return
  cd "$work" || return
  step git fetch -q "$dir/repo.git" "+refs/heads/run/$run:refs/remote-ci/run"
  # A CI slot never holds real work: discard edits a previous run left behind (formatters, codegen).
  step git checkout -q -f --detach "$sha"
  [ "$(git rev-parse HEAD)" = "$sha" ] || return 1
  step git clean -fdq
}
phase checkout checkout
[ -f "$env_file" ] && . "$env_file"
# Opt-in install caching. Legacy PREPARE commands still run unchanged.
if [ -n "${REMOTE_CI_INSTALL:-}" ]; then
  install_marker=$dir/slots/$(basename "$work").install
  hash_command=$REMOTE_CI_INSTALL
  install_hash=$(input_hash ${REMOTE_CI_INSTALL_INPUTS:-pnpm-lock.yaml}) || { finish 1; exit 1; }
  if [ -d node_modules ] && [ "$(cat "$install_marker" 2>/dev/null)" = "$install_hash" ]; then
    echo "== install cached"; echo "== timing install 0s"
  else
    rm -f "$install_marker"
    phase install supervised /bin/bash -c "$REMOTE_CI_INSTALL"
    printf '%s\n' "$install_hash" >"$install_marker"
  fi
fi
prepare_database() {
  [ "$REMOTE_CI_SLOTS" -gt 1 ] && acquire_guard
  if ! pg_ctl -D "$pgdata" status >/dev/null 2>&1; then
    pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w || {
      [ "$REMOTE_CI_SLOTS" -gt 1 ] && release_guard; return 1;
    }
  fi
  pg_started=1
  [ "$REMOTE_CI_SLOTS" -gt 1 ] && release_guard
  if [ "$role" != ci ]; then
    # Match the existing CI superuser privileges (tests provision runtime roles).
    # Password is sent on stdin, never as a process argument or log line.
    { printf "SELECT format('CREATE ROLE %%I LOGIN SUPERUSER PASSWORD %%L', '%s', '%s') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '%s') \gexec\n" "$role" "$(cat "$pgdata.pass")" "$role"; } |
      psql -d postgres -q -v ON_ERROR_STOP=1 || return
  fi
  dropdb --if-exists --force "$REMOTE_CI_DB" || return
  if [ -n "${REMOTE_CI_TEMPLATE_INPUTS:-}" ]; then
    : "${REMOTE_CI_DB_PREPARE:?template inputs require REMOTE_CI_DB_PREPARE}"
    hash_command=$REMOTE_CI_DB_PREPARE
    fingerprint=$(input_hash $REMOTE_CI_TEMPLATE_INPUTS) || return
    template=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_${fingerprint:0:32}
    mkdir -p "$dir/templates"
    template_lock=$dir/templates/$template.lock
    template_start=$(date +%s)
    until ! lock_live "$template_lock" && /usr/bin/shlock -f "$template_lock" -p $$; do sleep 1; done
    template_locked=1
    echo "== timing template-wait $(( $(date +%s) - template_start ))s"
    template_start=$(date +%s)
    if [ ! -f "$dir/templates/$template.ready" ] || [ "$(psql -d postgres -Atqc "select count(*) from pg_database where datname = '$template' and not datallowconn")" != 1 ]; then
      echo "== template build $template"
      rm -f "$dir/templates/$template.ready"
      dropdb --if-exists --force "$template" || return
      createdb -T template0 "$template" || return
      export "${REMOTE_CI_DATABASE_URL_VAR:-DATABASE_URL}=$(database_url "$template")"
      supervised /bin/bash -c "$REMOTE_CI_DB_PREPARE" || return
      # Prevent clients attaching to a published template; createdb can still clone it.
      psql -d postgres -qc "alter database \"$template\" with allow_connections false" || return
      touch "$dir/templates/$template.ready"
    else
      echo "== template cached $template"
    fi
    echo "== timing template $(( $(date +%s) - template_start ))s"
    clone_start=$(date +%s)
    createdb -O "$role" -T "$template" "$REMOTE_CI_DB" || return
    echo "== timing clone $(( $(date +%s) - clone_start ))s"
    rm -f "$template_lock"; template_locked=0; template_lock=""
  else
    createdb -O "$role" -T template0 "$REMOTE_CI_DB" || return
  fi
  export "${REMOTE_CI_DATABASE_URL_VAR:-DATABASE_URL}=$(database_url "$REMOTE_CI_DB" "$role")"
}
[ -n "${REMOTE_CI_POSTGRES:-}" ] && phase database prepare_database
[ -n "${REMOTE_CI_PREPARE:-}" ] && phase prepare supervised /bin/bash -c "$REMOTE_CI_PREPARE"
[ "$force" = 1 ] && export TURBO_FORCE=true
check=${command:-$REMOTE_CI_CHECK}
echo "== $check"
check_start=$(date +%s)
supervised /usr/bin/time -l nice -n 10 /bin/bash -c "$check"; rc=$?
echo "== timing check $(( $(date +%s) - check_start ))s"
echo "== timing run $(( $(date +%s) - started ))s (before cleanup)"
finish "$rc"
exit "$rc"
