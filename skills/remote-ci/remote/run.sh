#!/bin/bash
# ABOUTME: Checks one exact commit in an isolated slot with optional install and database templates.
# ABOUTME: Coordinates shared Postgres; REMOTE_CI_EXCLUSIVE=1 opts a project into the host-exclusive run.
# Usage: run.sh <project> <commit> <tree> <run-id> [<base64 command>] [<force>] [<mode>] [<pool generation>] [<first submitted epoch>]
set -u
runner_dir=$(cd "$(dirname "$0")" && pwd)
. "$runner_dir/lib.sh"
infra_exec() {
  local phase=$1 now remaining seconds timeout_code=74 allow=() recovery=(); shift
  now=$(date +%s)
  seconds=$REMOTE_CI_TIMEOUT
  if [ "$phase" = cleanup ]; then
    timeout_code=76
  else
    remaining=$((wait_deadline - now))
    [ "$remaining" -ge "$seconds" ] || seconds=$remaining
  fi
  if [ -n "${scheduler_phase_deadline:-}" ] && [ $((scheduler_phase_deadline - now)) -lt "$seconds" ]; then
    seconds=$((scheduler_phase_deadline - now))
  fi
  [ "$seconds" -gt 0 ] || return "$timeout_code"
  [ ! -f "$dir/results/$run.infra-tree" ] || return 76
  [ "${infra_allow_success_descendants:-0}" != 1 ] || allow=(--allow-success-descendants)
  [ "${infra_legacy_recovery:-0}" != 1 ] || recovery=(--legacy-cleanup)
  python3 "$runner_dir/bounded_process.py" --phase "$phase" --timeout "$seconds" \
    --timeout-code "$timeout_code" --reap-code 76 --ownership "$dir/results/$run.infra-tree" \
    --token "$resource_token" ${allow[@]+"${allow[@]}"} ${recovery[@]+"${recovery[@]}"} -- "$@"
}
infra_exec_service() { infra_allow_success_descendants=1 infra_exec "$@"; }
bounded_wrap_database() {
  local tool definition raw
  for tool in psql pg_ctl dropdb createdb; do
    raw=bounded_raw_$tool
    definition=$(declare -f "$tool")
    if [ -n "$definition" ]; then
      eval "${definition/#$tool /$raw }"
      export -f "$raw"
    else
      raw=$tool
    fi
    if [ "$tool" = pg_ctl ]; then
      eval "$tool() { case \" \$* \" in *' start '*) infra_exec_service \"\${scheduler_phase_name:-database}\" /bin/bash -c '\"\$@\"' _ $raw \"\$@\" ;; *) infra_exec \"\${scheduler_phase_name:-database}\" /bin/bash -c '\"\$@\"' _ $raw \"\$@\" ;; esac; }"
    else
      eval "$tool() { infra_exec \"\${scheduler_phase_name:-database}\" /bin/bash -c '\"\$@\"' _ $raw \"\$@\"; }"
    fi
  done
}
bounded_stop_idle_postgres() {
  local data major stop_rc
  slots_active && return 0
  for data in "$ci"/pg/*; do
    [ -f "$data/PG_VERSION" ] || continue
    major=$(basename "$data"); major=${major%-disposable}; major=${major%-minimal}
    infra_exec cleanup "$(platform_pgbin "$major")/pg_ctl" -D "$data" stop -m fast >/dev/null 2>&1
    stop_rc=$?
    [ "$stop_rc" != 76 ] || return 76
  done
}
if [ "${1:-}" = --cleanup ]; then
  cleanup_version=$(python3 "$runner_dir/resources.py" scheduler-version) || exit 74
  if [ "$cleanup_version" = v2 ]; then
    exec python3 "$runner_dir/scheduler_maintenance.py" recover-file "$2"
  fi
fi
if [ "${1:-}" = --cleanup-v2 ]; then
  python3 "$runner_dir/resources.py" verify --token "${AGENT_CI_TOKEN:-}" \
    --generation "${AGENT_CI_LEASE_GENERATION:-0}" --owner "$$" --phase cleanup || exit 74
fi
clean_databases() {
  local owner_filter="" databases db
  [ "$slotted" = 1 ] && owner_filter="and datdba = (select oid from pg_roles where rolname = '$role')"
  if [ -n "${REMOTE_CI_DB_CLEAN:-}" ]; then
    # Enumerate first: a psql|dropdb pipeline would run two infrastructure
    # supervisors concurrently against the same V1 ownership journal.
    databases=$(psql -d postgres -Atq -v pattern="$REMOTE_CI_DB_CLEAN" -v role="$role" <<SQL
select datname from pg_database
where datname like :'pattern' $owner_filter;
SQL
    ) || return 1
    while IFS= read -r db; do
      [ -n "$db" ] || continue
      # A test harness may mark its own clone source as a template.
      dropdb --if-exists --force "$db" ||
        { psql -d postgres -q -c "alter database \"$db\" is_template false" </dev/null && dropdb --if-exists --force "$db"; } || return 1
    done <<<"$databases"
  fi
  [ "$slotted" = 0 ] || dropdb --if-exists --force "$REMOTE_CI_DB"
}

# Recovery uses the failed run's settings, never the new candidate's settings.
if [ "${1:-}" = --cleanup ] || [ "${1:-}" = --cleanup-v2 ]; then
  cleanup_protocol=$1
  pending=$2; dir=${pending%/results/*}; run=${pending##*/}; run=${run%.cleanup}; run=${run%.recovery}
  [ -f "$dir/runs/$run/conf" ] || exit 76
  load_project "${dir##*/}" "$dir/runs/$run/conf" || exit 76
  wait_deadline=$(( $(date +%s) + 60 ))
  scheduler_phase_deadline=$wait_deadline scheduler_phase_name=cleanup
  [ "$cleanup_protocol" != --cleanup ] || bounded_wrap_database
  slot="" role="" database="" owner=""
  while IFS='=' read -r key value; do
    case "$key" in slot) slot=$value ;; role) role=$value ;; database) database=$value ;; pid) owner=$value ;; esac
  done <"$pending"
  case "$slot" in ''|*[!0-9]*|0) exit 76 ;; esac
  case "$role:$database" in *[!a-zA-Z0-9_:]*|:*|*:) exit 76 ;; esac
  REMOTE_CI_DB=$database
  if [ "$cleanup_protocol" = --cleanup-v2 ]; then
    # The maintenance supervisor owns the inherited quarantine leases and a
    # bounded process group. There is no second guard or nested admission.
    slotted=1
    if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
      pg_ctl -D "$pgdata" status >/dev/null 2>&1 ||
        pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w || exit 76
      clean_databases || exit 76
    fi
    git -C "$dir/repo.git" update-ref -d "refs/heads/run/$run" || exit 76
    exit 0
  fi
  # V1 recovery applies quarantine even when the ownership journal cannot reap.
  case "$owner" in ''|*[!0-9]*) ;; *) kill -0 "$owner" 2>/dev/null && exit 76 ;; esac
  python3 "$runner_dir/resources.py" reap --marker "$dir/results/$run.resource-stop" || true
  resource_token="$project-$run-$owner"
  python3 "$runner_dir/bounded_process.py" --reap-marker "$dir/results/$run.infra-tree" --token "$resource_token" || exit 76
  acquire_guard || exit 76
  trap 'release_guard' EXIT
  infra_legacy_recovery=1
  trap 'exit 76' HUP INT TERM
  [ -f "$pending" ] || exit 0
  if [ -z "$owner" ]; then
    for claim in "$dir"/results/tree-*.running.run; do
      [ -f "$claim" ] || continue
      read -r claim_pid claimed <"$claim"
      [ "$claimed" != "$run" ] || owner=$claim_pid
    done
  fi
  case "$owner" in ''|*[!0-9]*) ;; *) kill -0 "$owner" 2>/dev/null && exit 76 ;; esac
  if [ "$slotted" = 1 ]; then
    claimed=$(cat "$dir/slots/$slot.run" 2>/dev/null)
    [ -z "$claimed" ] || [ "$claimed" = "$run" ] || exit 76
    lock_live "$dir/slots/$slot.lock" && exit 76
  else
    slots_active && exit 76
  fi
  if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
    pg_profile_ensure || exit 76
    pg_ctl -D "$pgdata" status >/dev/null 2>&1 ||
      pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w || exit 76
    clean_databases || exit 76
  fi
  cache_owner=$owner
  for claim in "$dir"/results/tree-*.running.run; do
    [ -f "$claim" ] || continue
    read -r owner claimed <"$claim"
    [ "$claimed" = "$run" ] || continue
    acquire_lock "${claim%.run}.guard" || exit 76
    read -r owner claimed <"$claim"
    [ "$claimed" != "$run" ] || rm -f "$claim" "${claim%.run}"
    rm -f "${claim%.run}.guard"
  done
  infra_exec cleanup python3 "$runner_dir/cache.py" release --token "$project-$run-$cache_owner" || exit 76
  infra_exec cleanup git -C "$dir/repo.git" update-ref -d "refs/heads/run/$run" || exit 76
  python3 "$runner_dir/resources.py" release --token "$resource_token" || exit 76
  if [ "$slotted" = 1 ]; then rm -f "$dir/slots/$slot.lock" "$dir/slots/$slot.run"; fi
  rm -f "$pending" || exit 76
  echo "== cleanup recovered: $run"
  exit 0
fi
if [ -f "$runner_dir/../conf" ]; then
  project_conf=$runner_dir/../conf
  load_project "$1" "$runner_dir/../conf" || { echo 2 >"$dir/results/$4.exit"; exit 2; }
  env_file=$runner_dir/../env.local
else
  project_conf=$ci/projects/$1/conf
  load_project "$1" || { echo 2 >"$dir/results/$4.exit"; exit 2; }
  env_file=$dir/env.local
fi
sha=$2 tree=$3 run=$4
# Pool launches carry one durable generation; duplicate delivery cannot execute twice.
if [ -n "${8:-}" ]; then
  python3 "$runner_dir/platform_info.py" fence "$runner_dir/../assignment.started" "$8" || exit 3
fi
command=$(printf '%s' "${5:-}" | platform_decode 2>/dev/null)
database_config_error=0
if [ -n "$command" ] && [ -n "${REMOTE_CI_NO_DB_COMMANDS:-}" ]; then
  while IFS= read -r no_db_command || [ -n "$no_db_command" ]; do
    [ "$command" = "$no_db_command" ] || continue
    if [ -n "${REMOTE_CI_PREPARE:-}" ]; then
      if [ "${REMOTE_CI_NO_DB_PREPARE+x}" != x ]; then
        database_config_error=1
      else
        REMOTE_CI_PREPARE=$REMOTE_CI_NO_DB_PREPARE
      fi
    fi
    REMOTE_CI_POSTGRES=
    break
  done <<<"$REMOTE_CI_NO_DB_COMMANDS"
fi
force=${6:-0}
resource_mode=${7:-runner}
resource_token="$project-$run-$$"
resource_owned=0 resource_memory=0
scheduler_version=v1
cache() {
  local cache_command=(/usr/bin/env
    "REMOTE_CI_INSTALL=${REMOTE_CI_INSTALL:-}" "REMOTE_CI_INSTALL_INPUTS=${REMOTE_CI_INSTALL_INPUTS:-}"
    "REMOTE_CI_LIFECYCLE_INPUTS=${REMOTE_CI_LIFECYCLE_INPUTS:-}" "REMOTE_CI_INSTALL_OUTPUTS=${REMOTE_CI_INSTALL_OUTPUTS:-node_modules}"
    "REMOTE_CI_NODE=${REMOTE_CI_NODE:-}" "REMOTE_CI_PNPM=${REMOTE_CI_PNPM:-}" "REMOTE_CI_POSTGRES=${REMOTE_CI_POSTGRES:-}"
    "REMOTE_CI_PG_PROFILE=${REMOTE_CI_PG_PROFILE:?Postgres profile not resolved}" "REMOTE_CI_SERVICE_IMAGES=${REMOTE_CI_SERVICE_IMAGES:-}"
    "REMOTE_CI_ENV_REVISION=${REMOTE_CI_ENV_REVISION:-}" python3 "$runner_dir/cache.py")
  if [ "$scheduler_version" = v2 ] && [ "$locked" = 1 ] && [ "$resource_owned" = 1 ]; then
    scheduler_exec "${scheduler_phase_name:-check}" "${cache_command[@]}" "$@"
  elif [ "$scheduler_version" = v1 ] && [ "$resource_owned" = 1 ]; then
    infra_exec "${scheduler_phase_name:-check}" "${cache_command[@]}" "$@"
  else
    "${cache_command[@]}" "$@"
  fi
}
cache_delete() {
  if [ "$scheduler_version" = v2 ] && [ "$locked" = 1 ] && [ "$resource_owned" = 1 ]; then
    scheduler_exec "${scheduler_phase_name:-check}" /bin/bash "$runner_dir/prune.sh" --cache-delete "$1"
  elif [ "$scheduler_version" = v1 ]; then
    infra_exec "${scheduler_phase_name:-check}" /bin/bash "$runner_dir/prune.sh" --cache-delete "$1"
  else
    /bin/bash "$runner_dir/prune.sh" --cache-delete "$1"
  fi
}
log=$dir/logs/$run.log
exec >"$log" 2>&1
[ -z "$exclusive_notice" ] || echo "$exclusive_notice"
submitted=$(date +%s) first_submitted=""; launched=$submitted
# A pool move (arg 9) keeps the first submission time: queue age and total wait survive it.
case "${9:-}" in ''|*[!0-9]*) ;; *) [ "$9" -ge "$submitted" ] || { submitted=$9; first_submitted=$9; } ;; esac
wait_deadline=$((submitted + REMOTE_CI_WAIT_TIMEOUT))
child="" watchdog="" locked=0 slot=0 template_lock="" pg_started=0 guard_owned=0 template_locked=0 drain_owned=0
install_lock=""
claim=$dir/results/tree-$tree.running
claim_owned=0 cache_result=0 claim_guard_owned=0
# Legacy protocol-2 keys omit host tools and settings. Never reuse or publish them.
cache_identity=0
case "$tree" in ''|*[!0-9a-f]*) ;; *) [ "${#tree}" != 64 ] || cache_identity=1 ;; esac
check_rc="" cleanup_failed=0 candidate_failed=0
telemetry() {
  python3 "$runner_dir/resources.py" event --token "$resource_token" --run-id "$run" "$@" >/dev/null 2>&1 || true
}
telemetry --event queued --phase queue
work=$dir/work
base_db=$REMOTE_CI_DB
role=ci
# Receipt hooks: only structured metadata crosses this boundary.
receipt_file=$ci/receipts/$project/$run.json
# BSD time is optional. Linux/WSL and unknown hosts keep sampled metrics only.
receipt_bsd_time=0
[ "$platform_os" != Darwin ] || receipt_bsd_time=1
receipt() {
  # Bound the whole observer group, including a stuck SQL client or index lock.
  perl -e '
    my $pid=fork(); defined $pid or exit 0;
    if (!$pid) { setpgrp(0,0); exec @ARGV; exit 0; }
    $SIG{ALRM}=sub { kill "KILL", -$pid; kill "KILL", $pid; waitpid($pid,0); exit 0; };
    alarm 1; waitpid($pid,0);
  ' python3 "$runner_dir/receipts.py" "$1" "$receipt_file" "${@:2}" >/dev/null 2>&1 || true
}
receipt_mode=full; [ -z "$command" ] || receipt_mode=targeted
receipt begin "$project" "$run" "$receipt_mode" "$$"
receipt cache tree miss not-cached
python3 "$runner_dir/receipts.py" monitor "$receipt_file" "$$" "$dir/results/$run.resource-stop.tree" </dev/null >/dev/null 2>&1 &
receipt_sampler=$!
receipt_phase() {
  [ "$pg_started" = 0 ] || receipt postgres
  receipt phase "$1"
}
acquire_claim_guard() {
  [ "$claim_guard_owned" = 1 ] && return 0
  acquire_lock "$claim.guard" "${1:-}" || return 1
  claim_guard_owned=1
}
release_claim_guard() {
  [ "$claim_guard_owned" = 1 ] && rm -f "$claim.guard"
  claim_guard_owned=0
}
stop_watchdog() {
  local deadline=$(( SECONDS + 30 ))
  [ -n "$watchdog" ] || return 0
  # Let an in-flight sampler finish; signal only the watchdog until its deadline.
  kill -TERM "$watchdog" 2>/dev/null || true
  while kill -0 "$watchdog" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      kill -KILL -"$watchdog" 2>/dev/null || kill -KILL "$watchdog" 2>/dev/null || true
      echo "runner cleanup pending: watchdog shutdown deadline"
      echo 76 >"$dir/results/$run.resource-stop"
      telemetry --event stopped --reason cleanup-incomplete --exit-code 76
      cleanup_failed=1
      watchdog=""
      return 76
    fi
    sleep 0.1
  done
  wait "$watchdog" 2>/dev/null || true
  watchdog=""
}
finish() {
  local rc=${1:-1} cleanup_start published=0 claim_pid="" claim_run=""
  trap '' HUP INT TERM
  stop_watchdog || cleanup_failed=1
  if [ -n "$child" ]; then
    kill -TERM -"$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null
    sleep 1
    kill -KILL -"$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  if [ "$scheduler_version" != v2 ]; then
    python3 "$runner_dir/resources.py" reap --marker "$dir/results/$run.resource-stop" || cleanup_failed=1
  fi
  [ -n "${child_marker:-}" ] && rm -f "$child_marker"
  cleanup_start=$(date +%s)
  receipt_phase cleanup
  scheduler_phase_deadline=$((cleanup_start + 60))
  scheduler_phase_name=cleanup
  [ ! -f "$dir/results/$run.infra-tree" ] || cleanup_failed=1
  if [ "$cleanup_failed" = 0 ] && [ -n "$install_lock" ] && [ "$(cat "$install_lock" 2>/dev/null)" = "$$" ]; then
    cache_delete "$install_lock.child" || cleanup_failed=1
    cache_delete "$install_lock" || cleanup_failed=1
  fi
  [ "$cleanup_failed" != 0 ] || { [ "$template_locked" = 1 ] && rm -f "$template_lock.child"; }
  release_drain
  [ "$cleanup_failed" != 0 ] || { [ "$template_locked" = 1 ] && rm -f "$template_lock"; }
  # A queued/busy run has no resources and must never stop another run's Postgres.
  if [ "$cleanup_failed" = 0 ] && [ "$locked" = 1 ] && [ "$pg_started" = 1 ]; then
    clean_databases || { echo "Error: database cleanup failed"; cleanup_failed=1; }
  fi
  if [ "$pg_started" = 1 ]; then
    receipt pg-end
  fi
  [ -z "${receipt_turbo_dir:-}" ] || receipt turbo-after "$receipt_turbo_dir"
  [ "$cleanup_failed" = 0 ] || rc=76
  # Release pins before releasing the slot. Its next owner must not observe
  # stale pins on the previous job's dependency outputs.
  if [ "$cleanup_failed" = 0 ]; then
    if [ "$scheduler_version" = v2 ]; then
      scheduler_cache_release || { rc=74; cleanup_failed=1; }
    else
      [ "$resource_owned" != 1 ] || cache release --token "$resource_token" || { rc=76; cleanup_failed=1; }
      # Early rejection and queue expiry never acquired capacity for a scan.
      # Preserve cache infrastructure errors (74) versus capacity errors (75).
      if [ "$scheduler_version" = v1 ] && [ "$resource_owned" = 1 ] && [ "$resource_memory" -gt 0 ]; then
        cache prune || rc=$?
      fi
    fi
  fi
  if [ "$cleanup_failed" = 0 ] && [ "$resource_owned" = 1 ]; then
    if [ "$scheduler_version" = v1 ]; then
      infra_exec cleanup git -C "$dir/repo.git" update-ref -d "refs/heads/run/$run" 2>/dev/null
      [ "$?" != 76 ] || cleanup_failed=1
    else
      scheduler_exec cleanup git -C "$dir/repo.git" update-ref -d "refs/heads/run/$run" 2>/dev/null || cleanup_failed=1
    fi
  fi
  [ ! -f "$dir/results/$run.infra-tree" ] || cleanup_failed=1
  [ "$cleanup_failed" = 0 ] || rc=76
  if [ "$locked" = 1 ] && [ "$cleanup_failed" = 0 ]; then
    if [ "$scheduler_version" != v2 ] && [ "$slotted" = 1 ]; then
      if acquire_guard; then
        rm -f "$dir/slots/$slot.lock" "$dir/slots/$slot.run"
      else
        echo "lost-run: run $run cleanup guard unavailable after 60 seconds; slot retained"
        rc=74; cleanup_failed=1
      fi
    fi
    if [ "$guard_owned" = 1 ]; then
      if [ "$scheduler_version" = v2 ]; then stop_idle_postgres
      else bounded_stop_idle_postgres || cleanup_failed=1; fi
      [ ! -f "$dir/results/$run.infra-tree" ] || cleanup_failed=1
      if [ "$cleanup_failed" = 1 ] && [ "$scheduler_version" != v2 ] && [ "$slotted" = 1 ]; then
        platform_lock "$dir/slots/$slot.lock" $$ || true
        printf '%s\n' "$run" >"$dir/slots/$slot.run"
      fi
      release_guard
    fi
  fi
  [ "$cleanup_failed" = 0 ] || rc=76
  if [ "$cleanup_failed" = 1 ]; then
    printf 'run=%s\nslot=%s\ndatabase=%s\nrole=%s\ncheck_rc=%s\npid=%s\n' \
      "$run" "$slot" "$REMOTE_CI_DB" "$role" "$check_rc" "$$" >"$dir/results/$run.cleanup"
    echo "runner cleanup pending: $run"
  fi
  [ "$cleanup_failed" = 0 ] || release_guard
  if [ "$claim_owned" = 1 ] && [ "$cleanup_failed" = 0 ] && ! acquire_claim_guard "$scheduler_phase_deadline"; then
    echo "lost-run: run $run claim release guard unavailable after 60 seconds; claim retained"
    rc=74
  fi
  if [ "$resource_owned" = 1 ]; then
    if [ "$scheduler_version" = v2 ]; then
      quarantine=(); [ "$cleanup_failed" = 0 ] || quarantine=(--quarantine)
      actual=$(awk '/maximum resident set size/ {if ($1>peak) peak=$1} END {printf "%.0f", peak/1048576}' "$log")
      python3 "$runner_dir/resources.py" release --token "$resource_token" --result "$rc" --actual-memory "$actual" ${quarantine[@]+"${quarantine[@]}"} || rc=74
      echo "== memory predicted_mb=${resource_predicted:-0} actual_mb=$actual model=observe"
    else
      python3 "$runner_dir/resources.py" release --token "$resource_token" || rc=76
    fi
    resource_owned=0
  fi
  echo "== timing cleanup $(( $(date +%s) - cleanup_start ))s"
  echo "== timing total $(( $(date +%s) - submitted ))s (including queue)"
  if [ "$cache_result" = 1 ]; then
    if [ "$rc" = 0 ]; then echo "$sha" >"$dir/results/tree-$tree.pass.new" && mv "$dir/results/tree-$tree.pass.new" "$dir/results/tree-$tree.pass"
    else rm -f "$dir/results/tree-$tree.pass"; fi
  fi
  # The log is final before the result exists: callers run fail/blame as soon as they see it.
  echo "== finished rc=$rc $(date '+%F %T')"
  # Publish completion atomically after resource cleanup. Keep the claim guard
  # until publication and ownership-checked release have both completed.
  if echo "$rc" >"$dir/results/$run.exit.new" &&
     mv "$dir/results/$run.exit.new" "$dir/results/$run.exit"; then
    published=1
  else
    echo "lost-run: run $run terminal result could not be published (log $log)"
    echo "== finished rc=74 $(date '+%F %T')"
    rc=74
    [ "$cache_result" = 1 ] && rm -f "$dir/results/tree-$tree.pass"
  fi
  if [ "$claim_guard_owned" = 1 ]; then
    read -r claim_pid claim_run 2>/dev/null <"$claim.run" || true
    if [ "$published" = 1 ] && [ "$cleanup_failed" = 0 ] && [ "$claim_pid" = "$$" ] && [ "$claim_run" = "$run" ]; then
      rm -f "$claim.run" "$claim"
    fi
    release_claim_guard
  fi
  claim_owned=0
  # Result and log are final. Bound the tail and stop all receipt writers
  # before this process exits, so its owner can remove the run directory.
  kill -TERM "$receipt_sampler" 2>/dev/null || true
  receipt finalize "$rc"
  kill -KILL "$receipt_sampler" 2>/dev/null || true
  exit "$rc"
}
trap 'finish 74' HUP INT TERM
[ "$database_config_error" = 0 ] || { echo "remote-ci: no-DB command requires REMOTE_CI_NO_DB_PREPARE when REMOTE_CI_PREPARE is configured"; finish 74; }
# run_live PID [RUN]: 0 runner (of RUN, when given), 1 dead/invalid PID,
# 2 positively another program or another run's runner (a reused PID), 3 unobservable.
run_live() {
  local owner_command
  case "$1" in *[!0-9]* | "" | 0) return 1 ;; esac
  kill -0 "$1" 2>/dev/null || return 1
  owner_command=$(ps -p "$1" -o command= 2>/dev/null) || return 3
  [ -n "$owner_command" ] || return 3
  case "$owner_command" in *run.sh*) ;; *) return 2 ;; esac
  [ -n "${2:-}" ] || return 0
  case "$owner_command " in *" $2 "*) return 0 ;; *) return 2 ;; esac
}
busy_claim() { echo "busy: $*"; finish 75; }
cleanup_retried=0
pending_cleanup() {
  local pending remaining blocked="" deadline=$(( $(date +%s) + 60 ))
  [ "$deadline" -le "$wait_deadline" ] || deadline=$wait_deadline
  for pending in "$ci"/projects/*/results/*.cleanup; do
    [ -f "$pending" ] || continue
    remaining=$((deadline - $(date +%s)))
    if [ "$cleanup_retried" = 0 ] && [ "$remaining" -gt 0 ]; then
      # Bound the whole process group, including a stuck psql/dropdb or guard wait.
      perl -e '
        setpgrp(0,0); my $seconds=shift; my $pid=fork(); defined $pid or exit 76;
        if (!$pid) { setpgrp(0,0); exec @ARGV; exit 76; }
        $SIG{ALRM}=sub { kill "TERM", -$pid; sleep 1; kill "KILL", -$pid; waitpid($pid,0); exit 76; };
        alarm $seconds; waitpid($pid,0); exit($? ? 76 : 0);
      ' "$remaining" /bin/bash "$runner_dir/run.sh" --cleanup "$pending" || true
    fi
    [ ! -f "$pending" ] || [ -n "$blocked" ] || blocked=$pending
  done
  cleanup_retried=1
  if [ -n "$blocked" ]; then
    echo "runner cleanup pending: $(basename "$blocked" .cleanup)"
    release_guard; finish 76
  fi
}
# Cache hits need no slot or host resource. Check again after waiting below.
scheduler_version=$(python3 "$runner_dir/resources.py" scheduler-version) || finish 74
if [ "$scheduler_version" = v2 ]; then
  . "$runner_dir/scheduler.sh" || finish 74
else
  bounded_wrap_database
fi
if [ "$scheduler_version" = v2 ] && [ -z "$command" ] && [ "$force" = 0 ] && [ "$cache_identity" = 1 ] && cache pass-valid --path "$dir/results/tree-$tree.pass" &&
   ! compgen -G "$ci/projects/*/results/*.cleanup" >/dev/null; then
  receipt cache tree hit tree-pass
  echo "== cached: identical tree already passed"
  finish 0
fi
if [ "$scheduler_version" = v2 ]; then
  # Unknown execution identity must never coalesce live or completed V2 work.
  [ "$cache_identity" = 1 ] || tree=uncached-$run
  scheduler_admit
else
pending_cleanup
# shlock publishes the PID atomically. The sidecar binds that PID to a run ID;
# readers wait for matching metadata rather than following a previous owner.
if [ "$cache_identity" = 1 ] && [ -z "$command" ] && [ "$force" = 0 ]; then
  while :; do
    acquire_claim_guard "$wait_deadline" || busy_claim "tree claim guard wait reached the guard or total deadline"
    owner_pid="" followed=""
    read -r owner_pid followed 2>/dev/null <"$claim.run" || true
    owner=$(cat "$claim" 2>/dev/null)
    # Metadata binds the claim PID to a run ID; until it does, any runner may be the new owner.
    if [ "$owner" = "$owner_pid" ]; then run_live "$owner" "$followed"; else run_live "$owner"; fi; owner_state=$?
    if [ "$owner_state" = 3 ]; then
      release_claim_guard
      busy_claim "cannot inspect owner pid $owner; keeping tree claim"
    fi
    if [ "$owner_state" != 0 ] && [ -n "$followed" ] && run_child_live "$followed" "$owner_pid"; then
      release_claim_guard
      busy_claim "run $followed still has a live slot child (log $dir/logs/$followed.log)"
    fi
    if [ "$owner_state" = 2 ]; then
      echo "== stale tree claim (pid $owner is not its run.sh); replacing it"
      rm -f "$claim" "$claim.run"
    fi
    if platform_lock "$claim" $$; then
      if [ -n "$followed" ] && run_child_live "$followed" "$owner_pid"; then
        rm -f "$claim"
        release_claim_guard
        busy_claim "run $followed still has a live slot child (log $dir/logs/$followed.log)"
      fi
      claim_owned=1
      printf '%s %s\n' "$$" "$run" >"$claim.run.new"
      mv "$claim.run.new" "$claim.run"
      release_claim_guard
      break
    fi
    release_claim_guard
    [ $(( $(date +%s) - launched )) -lt 300 ] || busy_claim "tree claim metadata unavailable after 300 seconds"
    [ "$(date +%s)" -lt "$wait_deadline" ] || busy_claim "tree claim total wait exceeded $REMOTE_CI_WAIT_TIMEOUT seconds"
    if [ "$owner" = "$owner_pid" ] && [ -n "$followed" ] && [ "$owner_state" = 0 ]; then
      receipt cache tree follower following
      echo "== following run $followed sha $sha (log $dir/logs/$followed.log)"
      while [ ! -f "$dir/results/$followed.exit" ]; do
        [ "$(date +%s)" -lt "$wait_deadline" ] ||
          busy_claim "followed run $followed total wait exceeded $REMOTE_CI_WAIT_TIMEOUT seconds (log $dir/logs/$followed.log)"
        run_live "$owner" "$followed"; owner_state=$?
        if [ "$owner_state" = 1 ] || [ "$owner_state" = 2 ]; then
          # Recheck publication after death: finish writes the exit before releasing its claim.
          [ -f "$dir/results/$followed.exit" ] && break
          echo "lost-run: followed run $followed ended without a result (log $dir/logs/$followed.log)"
          finish 74
        fi
        sleep 1
      done
      rc=$(cat "$dir/results/$followed.exit")
      [ "$rc" = 0 ] || echo "Error: followed run rc=$rc (log $dir/logs/$followed.log)"
      finish "$rc"
    fi
    sleep 1
  done
fi
mkdir -p "$dir/slots"
# A v1 live claim still needs its metadata/deadline checks. Once resolved, a
# cached result needs no slot wait (and no host resource reservation).
if [ -z "$command" ] && [ "$force" = 0 ] && [ "$cache_identity" = 1 ] && cache pass-valid --path "$dir/results/tree-$tree.pass"; then
  receipt cache tree hit tree-pass
  echo "== cached: identical tree passed while queued"
  finish 0
fi
# Drain: the first waiting exclusive run (REMOTE_CI_EXCLUSIVE=1) claims $ci/drain; slot runs do not start while a
# live drain exists, so an exclusive run cannot starve behind a stream of slot runs, and
# later exclusive runs wait behind the drain owner. The drain owner holds no admission
# or slot while it waits, so waiting on it cannot deadlock.
slot_wait_logged=0
while [ "$locked" = 0 ]; do
  if [ "$slotted" = 0 ]; then
    if [ "$drain_owned" = 0 ] && ! lock_live "$ci/drain" && platform_lock "$ci/drain" $$; then
      drain_owned=1
      printf '%s\n' "$submitted" >"$ci/drain.at"
    fi
  fi
  # Only runs submitted after the drain owner yield to it (first come, first served
  # between slot and exclusive runs). A missing timestamp counts as oldest.
  drain_at=$(cat "$ci/drain.at" 2>/dev/null); case "$drain_at" in ''|*[!0-9]*) drain_at=0 ;; esac
  if [ "$drain_owned" = 1 ] || ! lock_live "$ci/drain" || { [ "$slotted" = 1 ] && [ "$submitted" -lt "$drain_at" ]; }; then
  if ! lock_live "$ci/lock" && platform_lock "$ci/lock" $$; then
    guard_owned=1
    # A dead PID is reclaimable, but its failed cleanup still owns the resources.
    pending_cleanup
    if [ "$slotted" = 0 ]; then
      if ! slots_active; then locked=1; slot=1; release_drain; fi
    else
      for ((candidate=1; candidate<=REMOTE_CI_SLOTS; candidate++)); do
        if ! lock_live "$dir/slots/$candidate.lock" && platform_lock "$dir/slots/$candidate.lock" $$; then
          slot=$candidate; locked=1
          printf '%s\n' "$run" >"$dir/slots/$slot.run"
          # One slot cannot collide with itself: it keeps the warm work checkout and the base
          # database name (some projects require their exact name, e.g. a _test suffix).
          if [ "$REMOTE_CI_SLOTS" -gt 1 ]; then
            work=$dir/work-slot$slot
            REMOTE_CI_DB=${base_db}_s$slot
            if [ "${#REMOTE_CI_DB}" -gt 63 ]; then
              REMOTE_CI_DB=mci_$(printf '%s' "$base_db" | shasum -a 256 | cut -c1-24)_s$slot
            fi
          fi
          role=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_s$slot
          break
        fi
      done
    fi
    if [ "$slotted" = 1 ] || [ "$locked" = 0 ]; then release_guard; fi
  fi
  fi
  [ "$locked" = 1 ] && break
  if [ "$slot_wait_logged" = 0 ]; then
    wait_reason=project-slot; [ "$slotted" = 1 ] || wait_reason=lease
    telemetry --event blocked --phase queue --reason "$wait_reason"
    slot_wait_logged=1
  fi
  if [ "$(date +%s)" -ge "$wait_deadline" ]; then
    echo "busy: slot wait reached total deadline (or exclusive runner/prune active)"
    finish 75
  fi
  sleep 2
done
if [ "$cache_identity" = 1 ] && [ -z "$command" ] && [ "$force" = 0 ] && cache pass-valid --path "$dir/results/tree-$tree.pass"; then
  receipt cache tree hit tree-pass
  echo "== cached: identical tree passed while queued"
  finish 0
fi
# Admission is host-wide, before any checkout/install/database work, and comes after
# the slot/exclusive lock: a job that holds an admission must never sit waiting on a lock
# (that idled one of AGENT_CI_MAX_JOBS while every other repository queued behind it).
# Tree followers above and cached trees share a result without a resource reservation.
resource_owned=1
echo "== resources queued"
allocation=$(python3 "$runner_dir/resources.py" acquire --token "$resource_token" --run-id "$run" --submitted "$submitted" --owner "$$" \
  --deadline "$wait_deadline" --mode "$resource_mode" --workers "${REMOTE_CI_WORKERS:-2}" --memory-mb "${REMOTE_CI_MEMORY_MB:-0}"); resource_rc=$?
[ "$resource_rc" = 0 ] || finish "$resource_rc"
read -r resource_workers resource_memory <<<"$allocation"
receipt grant "$resource_workers" "$resource_memory"
echo "== resources workers=$resource_workers memory_mb=$resource_memory"
fi  # v1 admission
[ "$cache_identity" = 1 ] && [ -z "$command" ] && cache_result=1
started=$(date +%s)
echo "== $project run $run sha $sha slot $slot/$([ "$slotted" = 1 ] && echo "$REMOTE_CI_SLOTS" || echo exclusive) start $(date '+%F %T')"
echo "== timing queue $((started - submitted))s"
step() { local step_rc; echo "== $*"; "$@"; step_rc=$?; [ "$step_rc" = 0 ] || { [ "$step_rc" = 76 ] && finish 76; finish 74; }; }
phase() {
  local label=$1 start rc scheduler_phase_deadline; shift
  start=$(date +%s)
  receipt_phase "$label"

  scheduler_phase_deadline=$((start + REMOTE_CI_TIMEOUT))
  "$@"; rc=$?
  echo "== timing $label $(( $(date +%s) - start ))s"
  if [ "$rc" != 0 ]; then
    case "$candidate_failed:$label:$rc" in 1:*) rc=1 ;; *:74|*:75|*:76) ;; 0:checkout:*|0:database:*) rc=74 ;; esac
    finish "$rc"
  fi
}
supervised() {
  local rc timing_available=0 supervised_seconds=$REMOTE_CI_TIMEOUT now
  if [ "$scheduler_version" = v2 ]; then
    # V2 keeps the receipt sampler; its pipe-gated child owns its descriptors.
    receipt timing-unavailable
    scheduler_exec check "$@"
    return $?
  fi
  now=$(date +%s)
  [ $((wait_deadline - now)) -ge "$supervised_seconds" ] || supervised_seconds=$((wait_deadline - now))
  if [ -n "${scheduler_phase_deadline:-}" ] && [ $((scheduler_phase_deadline - now)) -lt "$supervised_seconds" ]; then
    supervised_seconds=$((scheduler_phase_deadline - now))
  fi
  [ "$supervised_seconds" -gt 0 ] || return 74
  # time opens -o before exec. Give it an already-open descriptor, or run bare.
  if [ "$receipt_bsd_time" = 1 ] && { exec 9>"$dir/results/$run.time"; } 2>/dev/null; then
    timing_available=1
    perl -e 'setpgrp(0,0); exec @ARGV' /usr/bin/time -l -o /dev/fd/9 "$@" &
  else
    perl -e 'setpgrp(0,0); exec @ARGV' "$@" &
  fi
  child=$!
  exec 9>&-
  if [ "$slotted" = 1 ]; then child_marker=$dir/slots/$slot.lock.child; else child_marker=$ci/lock.child; fi
  printf '%s\n' "$child" >"$child_marker"
  [ "$template_locked" = 1 ] && printf '%s\n' "$child" >"$template_lock.child"
  [ -z "$install_lock" ] || printf '%s\n' "$child" >"$install_lock.child"
  python3 "$runner_dir/resources.py" mark --token "$resource_token" --marker "$child_marker" || {
    kill -TERM -"$child" 2>/dev/null; sleep 1; kill -KILL -"$child" 2>/dev/null; return 74;
  }
  : >"$dir/results/$run.resource-stop"
  perl -e 'setpgrp(0,0); exec @ARGV' python3 "$runner_dir/resources.py" watch \
    --group "$child" --token "$resource_token" --run-id "$run" --timeout "$supervised_seconds" --memory "$resource_memory" \
    --marker "$dir/results/$run.resource-stop" &
  watchdog=$!
  wait "$child"; rc=$?
  # Reap surviving descendants before slot reuse, including on a failed command.
  kill -TERM -"$child" 2>/dev/null || true
  if kill -0 -"$child" 2>/dev/null; then sleep 1; kill -KILL -"$child" 2>/dev/null || true; fi
  rm -f "$child_marker"
  [ "$template_locked" = 1 ] && rm -f "$template_lock.child"
  child=""
  [ -z "$install_lock" ] || cache_delete "$install_lock.child"
  stop_watchdog || cleanup_failed=1
  if [ "$timing_available" = 1 ]; then receipt rusage "$dir/results/$run.time"
  else receipt timing-unavailable; fi
  if [ -s "$dir/results/$run.resource-stop" ]; then
    resource_stop=$(cat "$dir/results/$run.resource-stop")
    return "$resource_stop"
  fi
  return "$rc"
}
candidate() {
  local stage=$1 command=$2 rc; shift 2
  candidate_failed=0
  supervised "$@" /bin/bash -c '
    /bin/bash -c "$1"; rc=$?
    printf "%s\n" "$rc" >"$2" || exit 74
    exit "$rc"
  ' _ "$command" "$dir/results/$run.check"; rc=$?
  [ ! -s "$dir/results/$run.resource-stop" ] || return "$(cat "$dir/results/$run.resource-stop")"
  check_rc=$(cat "$dir/results/$run.check" 2>/dev/null) || return 74
  echo "== check-result rc=$check_rc"
  [ "$rc" = "$check_rc" ] || return 74
  case "$stage:$rc" in install:126|install:127) return 74 ;; esac
  [ "$rc" = 0 ] || { candidate_failed=1; return 1; }
}
checkout() {
  if [ "$scheduler_version" = v2 ]; then
    local rc
    scheduler_exec check /bin/bash -c '
      set -e
      work=$1 repo=$2 run=$3 sha=$4
      [ -d "$work/.git" ] || git init -q "$work"
      cd "$work"
      git fetch -q "$repo" "+refs/heads/run/$run:refs/remote-ci/run"
      git checkout -q -f --detach "$sha"
      [ "$(git rev-parse HEAD)" = "$sha" ]
      git clean -fdq
    ' _ "$work" "$dir/repo.git" "$run" "$sha"; rc=$?
    [ "$rc" = 0 ] || return "$rc"
    cd "$work" || return
    return 0
  fi
  [ -d "$work/.git" ] || infra_exec checkout git init -q "$work" || return
  cd "$work" || return
  step infra_exec checkout git fetch -q "$dir/repo.git" "+refs/heads/run/$run:refs/remote-ci/run"
  # A CI slot never holds real work: discard edits a previous run left behind (formatters, codegen).
  step infra_exec checkout git checkout -q -f --detach "$sha"
  [ "$(infra_exec checkout git rev-parse HEAD)" = "$sha" ] || return 1
  step infra_exec checkout git clean -fdq
}
phase checkout checkout
[ "$scheduler_version" != v2 ] || scheduler_wrap_database
# Enforce the grant after candidate exports; custom runners consume AGENT_CI_WORKERS.
export AGENT_CI_ADMITTED=1 AGENT_CI_WORKERS=$resource_workers AGENT_CI_TOKEN=$resource_token
case "${VITEST_MAX_WORKERS:-}" in ''|*[!0-9]*) VITEST_MAX_WORKERS=$resource_workers ;; esac
[ "$VITEST_MAX_WORKERS" -gt 0 ] && [ "$VITEST_MAX_WORKERS" -le "$resource_workers" ] || VITEST_MAX_WORKERS=$resource_workers
export VITEST_MAX_WORKERS
export CMAKE_BUILD_PARALLEL_LEVEL=$resource_workers CARGO_BUILD_JOBS=$resource_workers MAKEFLAGS="-j$resource_workers"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 NUMEXPR_NUM_THREADS=1
receipt_turbo_dir=${TURBO_CACHE_DIR:-$work/.turbo}
receipt turbo-before "$receipt_turbo_dir"
receipt cache install disabled not-configured
receipt cache template disabled not-configured
# Shared immutable images; no slot-local pass marker is trusted.
export npm_config_cache="$ci/cache/npm" npm_config_store_dir="$ci/cache/pnpm" PIP_CACHE_DIR="$ci/cache/pip"
mkdir -p "$npm_config_cache" "$npm_config_store_dir" "$PIP_CACHE_DIR"
for store in npm pnpm pip; do
  cache register --pin-existing --key "store-$store" --kind "$store" \
    --path "$ci/cache/$store" --token "$resource_token" || finish 74
done
[ "$scheduler_version" = v2 ] || cache prune || finish 75
if [ -n "${REMOTE_CI_INSTALL:-}" ]; then
  install_hash=$(cache install-key --env-file "$env_file") || finish 74
  install_outputs=$(cache outputs --work "$work") || finish 74
  cache clean-work --work "$work" || finish 74
  install_lock=$ci/cache/build-$install_hash.lock
  until ! lock_live "$install_lock" && platform_lock "$install_lock" $$; do
    [ "$(date +%s)" -lt "$wait_deadline" ] || finish 75
    sleep 1
  done
  # Pin and account for outputs before an installer can leave partial files.
  for output in $install_outputs; do
    cache register --key "job-$project-$slot-$(printf '%s' "$work/$output" | shasum -a 256 | cut -c1-32)" \
      --kind deps --path "$work/$output" --token "$resource_token" || finish 74
  done
  # Publication and restore use the host ledger lock; each job gets writable files.
  if cache restore --key "$install_hash" --work "$work" --token "$resource_token"; then
    receipt_phase install
    receipt cache install hit input-match
    echo "== timing install 0s"
  else
    receipt cache install miss inputs-changed-or-missing
    # A missing or corrupt image always runs the real install.
    for output in $install_outputs; do
      cache_delete "$work/$output" || finish 74
    done
    cache preflight || finish 75
    phase install candidate install "$REMOTE_CI_INSTALL"
    cache publish --key "$install_hash" --work "$work" --token "$resource_token" || finish "$?"
    # The installer may hardlink to its store. The first job also needs a private copy.
    cache restore --key "$install_hash" --work "$work" --token "$resource_token" || finish "$?"
  fi
  cache_delete "$install_lock" || finish 74
  install_lock=""
fi
# Explicit shared Turbo paths must be inside ~/ci so the host can account for them.
for cache_path in "$work/.turbo" ${REMOTE_CI_CACHE_PATHS:-} ${TURBO_CACHE_DIR:-}; do
  mkdir -p "$cache_path" || finish 74
  cache_id=$(printf '%s' "$cache_path" | shasum -a 256 | cut -c1-32)
  cache register --key "turbo-$cache_id" --kind turbo \
    --path "$cache_path" --token "$resource_token" || finish 74
done
[ "$scheduler_version" = v2 ] || cache prune || finish 75
prepare_database() {
  [ "$scheduler_version" != v2 ] || pg_started=1
  if [ "$scheduler_version" != v2 ] && [ "$slotted" = 1 ] && ! acquire_guard; then
    echo "lost-run: run $run database guard unavailable after 60 seconds"
    return 74
  fi
  pg_profile_ensure || return
  if ! pg_ctl -D "$pgdata" status >/dev/null 2>&1; then
    pg_ctl -D "$pgdata" -l "$ci/pg/$REMOTE_CI_POSTGRES.log" start -w || {
      [ "$slotted" = 1 ] && release_guard; return 1;
    }
  fi
  pg_started=1
  receipt postgres
  [ "$slotted" = 1 ] && release_guard
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
    fingerprint=$(printf '%s\n' "$fingerprint" "${REMOTE_CI_PG_PROFILE:?Postgres profile not resolved}" "$(cat "$pgdata/cache-generation" 2>/dev/null)" | shasum -a 256 | cut -c1-64)
    template=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_${fingerprint:0:32}
    mkdir -p "$dir/templates"
    template_lock=$dir/templates/$template.lock
    receipt_phase template
    template_start=$(date +%s)
    if [ "$scheduler_version" != v2 ]; then
    until ! lock_live "$template_lock" && platform_lock "$template_lock" $$; do
      [ "$(date +%s)" -lt "$wait_deadline" ] || { echo "busy: template total wait exceeded $REMOTE_CI_WAIT_TIMEOUT seconds"; return 75; }
      sleep 1
    done
    template_locked=1
    fi
    echo "== timing template-wait $(( $(date +%s) - template_start ))s"
    template_start=$(date +%s)
    if [ ! -f "$dir/templates/$template.ready" ] || [ "$(psql -d postgres -Atqc "select count(*) from pg_database where datname = '$template' and not datallowconn")" != 1 ]; then
      receipt cache template miss template-missing
      echo "== template build $template"
      rm -f "$dir/templates/$template.ready"
      dropdb --if-exists --force "$template" || return
      cache preflight || return 75
      createdb -T template0 "$template" || return
      export "${REMOTE_CI_DATABASE_URL_VAR:-DATABASE_URL}=$(database_url "$template")"
      candidate migration "$REMOTE_CI_DB_PREPARE" || return
      # Prevent clients attaching to a published template; createdb can still clone it.
      psql -d postgres -qc "alter database \"$template\" with allow_connections false" || return
      touch "$dir/templates/$template.ready"
    else
      receipt cache template hit template-ready
      echo "== template cached $template"
      touch "$dir/templates/$template.ready"   # prune keeps recently used templates
    fi
    echo "== timing template $(( $(date +%s) - template_start ))s"
    template_bytes=$(psql -d postgres -Atqc "select pg_database_size('$template')") || return
    template_meta=$(python3 -c 'import json,sys; print(json.dumps(dict(database=sys.argv[1],port=sys.argv[2],dropdb=sys.argv[3])))' "$template" "$pgport" "$pgbin/dropdb") || return
    cache register --key "$template" --kind template \
      --path "$dir/templates/$template.ready" --size "$template_bytes" --metadata "$template_meta" --token "$resource_token" || return
    receipt_phase clone
    clone_start=$(date +%s)
    cache preflight --size "$template_bytes" || return 75
    pg_clone_database "$role" "$template" "$REMOTE_CI_DB" || return
    echo "== timing clone $(( $(date +%s) - clone_start ))s"
    [ "$template_locked" != 1 ] || rm -f "$template_lock"
    template_locked=0; template_lock=""
  else
    cache preflight || return 75
    createdb -O "$role" -T template0 "$REMOTE_CI_DB" || return
  fi
  receipt_phase database
  export "${REMOTE_CI_DATABASE_URL_VAR:-DATABASE_URL}=$(database_url "$REMOTE_CI_DB" "$role")"
}
[ -n "${REMOTE_CI_POSTGRES:-}" ] && phase database prepare_database
[ -n "${REMOTE_CI_PREPARE:-}" ] && phase prepare candidate prepare "$REMOTE_CI_PREPARE"
[ "$force" = 1 ] && export TURBO_FORCE=true
check=${command:-$REMOTE_CI_CHECK}
echo "== $check"
check_start=$(date +%s)
receipt_phase check
check_prefix=(nice -n 10)
[ "$receipt_bsd_time" != 1 ] || check_prefix=("${platform_time[@]}" "${check_prefix[@]}")
candidate check "$check" "${check_prefix[@]}"; rc=$?
echo "== timing check $(( $(date +%s) - check_start ))s"
echo "== timing run $(( $(date +%s) - started ))s (before cleanup)"
finish "$rc"
