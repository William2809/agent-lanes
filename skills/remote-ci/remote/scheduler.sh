# V2 hooks. Sourced once per run; the selected version never changes mid-run.
scheduler_admit() {
  local targeted=0 request=$dir/results/$run.request.json
  [ -z "$command" ] || targeted=1
  export SCHEDULER_MEMORY_MB=${REMOTE_CI_MEMORY_MB:-0}
  export SCHEDULER_TARGETED=$targeted SCHEDULER_WORKERS=${REMOTE_CI_WORKERS:-2}
  export SCHEDULER_SLOTS=$REMOTE_CI_SLOTS SCHEDULER_EXCLUSIVE=${REMOTE_CI_EXCLUSIVE:-0}
  export SCHEDULER_LEASES=${REMOTE_CI_LEASES:-} SCHEDULER_POSTGRES=${REMOTE_CI_POSTGRES:-}
  export SCHEDULER_CONTEXT_FILE=$project_conf
  printf '%s' "${command:-$REMOTE_CI_CHECK}" |
    python3 "$runner_dir/scheduler_request.py" "$project" "$sha" "$tree" "$run" "$dir/runs/$run/submission.json" >"$request.new" || finish 74
  mv "$request.new" "$request" || finish 74
  resource_profile=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["profile"])' "$request") || finish 74
  receipt profile "$resource_profile"
  resource_owned=1
  echo "== resources queued scheduler=v2"
  allocation=$(python3 "$runner_dir/resources.py" acquire --token "$resource_token" --owner "$$" \
    --deadline "$wait_deadline" --mode "$resource_mode" --request-file "$request" \
    ${first_submitted:+--submitted "$first_submitted"}); resource_rc=$?
  # 78 means a followed execution passed. It is not a grant to execute.
  # 79: a pool client withdrew this queued run to move it to another runner. Nothing ran.
  [ "$resource_rc" != 78 ] || finish 0
  [ "$resource_rc" = 0 ] || finish "$resource_rc"
  read -r resource_workers resource_memory slot resource_predicted scheduler_job_deadline scheduler_lane <<<"$allocation"
  if [ "${scheduler_job_deadline:-0}" -gt 0 ] && [ "$scheduler_job_deadline" -lt "$wait_deadline" ]; then
    wait_deadline=$scheduler_job_deadline
  fi
  echo "== scheduler lane=${scheduler_lane:-normal}"
  receipt grant "$resource_workers" "$resource_memory"
  locked=1
  slotted=1
  mkdir -p "$dir/slots"
  work=$dir/work-slot$slot
  # A placeholder preserves required suffixes, for example app_s{slot}_test.
  case "$base_db" in
    *'{slot}'*) REMOTE_CI_DB=${base_db//\{slot\}/$slot} ;;
    *) REMOTE_CI_DB=$base_db; [ "$REMOTE_CI_SLOTS" = 1 ] || REMOTE_CI_DB=${base_db}_s$slot ;;
  esac
  case "$REMOTE_CI_DB" in ''|*[!a-zA-Z0-9_]*) finish 74 ;; esac
  [ "${#REMOTE_CI_DB}" -le 63 ] || finish 74
  role=mci_$(printf '%s' "$project" | shasum -a 256 | cut -c1-12)_s$slot
  if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
    printf 'run=%s\nslot=%s\ndatabase=%s\nrole=%s\n' "$run" "$slot" "$REMOTE_CI_DB" "$role" >"$dir/results/$run.recovery.new" &&
      mv "$dir/results/$run.recovery.new" "$dir/results/$run.recovery" || finish 74
  fi
  echo "== scheduler mode=$([ "$targeted" = 1 ] && echo run || echo full)"
  echo "== resources workers=$resource_workers memory_mb=$resource_memory predicted_mb=$resource_predicted model=observe"
  if [ -z "$command" ] && [ "$force" = 0 ] && [ "$cache_identity" = 1 ] && cache pass-valid --path "$dir/results/tree-$tree.pass"; then
    receipt cache tree hit tree-pass
    echo "== cached: identical tree passed while queued"
    finish 0
  fi
}
# Postgres start and cleanup already hold the cluster lease. These are no-ops,
# not a second lock protocol. Keep the cluster warm; legacy prune must be stopped.
acquire_guard() { return 0; }
release_guard() { return 0; }
stop_idle_postgres() { return 0; }

scheduler_exec() {
  local phase=$1 seconds=$REMOTE_CI_TIMEOUT remaining; shift
  if [ "$phase" = cleanup ] && [ "$seconds" -gt 60 ]; then seconds=60; fi
  if [ -n "${scheduler_phase_deadline:-}" ]; then
    remaining=$((scheduler_phase_deadline - $(date +%s)))
    [ "$remaining" -gt 0 ] || return 74
    [ "$remaining" -ge "$seconds" ] || seconds=$remaining
  fi
  python3 "$runner_dir/scheduler_process.py" --token "$resource_token" --phase "$phase" \
    --timeout "$seconds" --marker "$dir/results/$run.resource-stop" -- "$@"
}
scheduler_db() {
  local tool=$1; shift
  if declare -F "scheduler_raw_$tool" >/dev/null; then tool=scheduler_raw_$tool; fi
  scheduler_exec "${scheduler_phase_name:-database}" /bin/bash -c '"$@"' _ "$tool" "$@"
}
scheduler_wrap_database() {
  local tool definition
  for tool in psql pg_ctl dropdb createdb; do
    # Preserve fixture/host wrappers without exporting credentials or commands
    # into the queue. In normal use these resolve to the selected PG binaries.
    definition=$(declare -f "$tool")
    if [ -n "$definition" ]; then
      eval "${definition/#$tool /scheduler_raw_$tool }"
      export -f "scheduler_raw_$tool"
    fi
    eval "$tool() { scheduler_db $tool \"\$@\"; }"
  done
}

scheduler_cache_release() {
  local scheduler_phase_name=cleanup
  cache release --token "$resource_token"
}
