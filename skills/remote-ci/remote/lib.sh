# ABOUTME: Shared helpers for the remote-ci runner scripts on the runner machine (sourced, not executed).
# ABOUTME: Loads a project's config and puts its Node, pnpm and Postgres on PATH.
ci=$HOME/ci
load_project() {
  project=$1
  dir=$ci/projects/$project
  # shellcheck disable=SC1091
  . "${2:-$dir/conf}"
  : "${REMOTE_CI_CHECK:=pnpm run check}" "${REMOTE_CI_TIMEOUT:=1500}" "${REMOTE_CI_WAIT_TIMEOUT:=10800}" "${REMOTE_CI_SLOTS:=1}"
  case "$REMOTE_CI_WAIT_TIMEOUT" in *[!0-9]* | "" | 0*) echo "REMOTE_CI_WAIT_TIMEOUT must be a positive integer" >&2; return 2 ;; esac
  case "$REMOTE_CI_SLOTS" in *[!0-9]* | "" | 0*) echo "REMOTE_CI_SLOTS must be a positive integer" >&2; return 2 ;; esac
  if [ -n "${REMOTE_CI_TEMPLATE_INPUTS:-}" ] && { [ -z "${REMOTE_CI_POSTGRES:-}" ] || [ -z "${REMOTE_CI_DB_PREPARE:-}" ]; }; then
    echo "template inputs require REMOTE_CI_POSTGRES and REMOTE_CI_DB_PREPARE" >&2; return 2
  fi
  : "${REMOTE_CI_DB:=$(printf '%s' "$project" | tr -c 'a-zA-Z0-9\n' '_')_ci}"
  path=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin
  [ -n "${REMOTE_CI_NODE:-}" ] && path=$ci/node/$REMOTE_CI_NODE/bin:$path
  if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
    pgbin=/opt/homebrew/opt/postgresql@$REMOTE_CI_POSTGRES/bin
    pgdata=$ci/pg/$REMOTE_CI_POSTGRES
    pgport=$((5400 + REMOTE_CI_POSTGRES))
    path=$pgbin:$path
    export PGHOST=127.0.0.1 PGPORT=$pgport PGUSER=ci PGPASSFILE=$ci/pgpass
  fi
  export PATH=$path COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1 LC_ALL=en_US.UTF-8
}
database_url() {
  printf 'postgresql://%s:%s@127.0.0.1:%s/%s' "${2:-ci}" "$(cat "$pgdata.pass")" "$pgport" "$1"
}

# shlock reclaims dead PIDs; liveness checks must also ignore stale lock files.
lock_live() {
  local pid
  if [ -f "$1" ]; then
    pid=$(cat "$1")
    case "$pid" in *[!0-9]* | "" | 0) ;; *) kill -0 "$pid" 2>/dev/null && return 0 ;; esac
  fi
  # A SIGKILLed supervisor may leave a bounded child phase alive until its watchdog expires.
  child_live "$1.child"
}
child_live() {
  local group
  group=$(cat "$1" 2>/dev/null)
  case "$group" in *[!0-9]* | "" | 0) return 1 ;; esac
  kill -0 -"$group" 2>/dev/null
}
run_child_live() {
  local file
  for file in "$dir"/slots/*.run; do
    [ "$(cat "$file" 2>/dev/null)" = "$1" ] || continue
    child_live "${file%.run}.lock.child" && return 0
  done
  # Exclusive runs store their child's group beside the global PID lock.
  [ "$(cat "$ci/lock" 2>/dev/null)" = "$2" ] && child_live "$ci/lock.child"
}
slots_active() {
  local f
  for f in "$ci"/projects/*/slots/*.lock; do lock_live "$f" && return 0; done
  return 1
}
# Each guard attempt has its own bound, including during final cleanup.
acquire_lock() {
  local deadline=$(( $(date +%s) + 60 ))
  if [ -n "${2:-}" ] && [ "$2" -lt "$deadline" ]; then deadline=$2; fi
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! lock_live "$1" && /usr/bin/shlock -f "$1" -p $$; then return 0; fi
    sleep 1
  done
  return 1
}
acquire_guard() {
  [ "${guard_owned:-0}" = 1 ] && return 0
  acquire_lock "$ci/lock" || return 1
  guard_owned=1
}
release_guard() {
  if [ "${guard_owned:-0}" = 1 ] && [ "$(cat "$ci/lock" 2>/dev/null)" = "$$" ]; then
    rm -f "$ci/lock"
  fi
  guard_owned=0
}
stop_idle_postgres() {
  local data major
  slots_active && return 0
  for data in "$ci"/pg/*; do
    [ -f "$data/PG_VERSION" ] || continue
    major=$(basename "$data")
    /opt/homebrew/opt/postgresql@"$major"/bin/pg_ctl -D "$data" stop -m fast >/dev/null 2>&1 || true
  done
}
# Hash names and Git blob IDs, not mtimes. Include commands and toolchain identity.
input_hash() {
  local inputs
  inputs=$(git ls-files -s -- "$@") || return
  [ -n "$inputs" ] || { echo "cache inputs matched no tracked files" >&2; return 1; }
  { printf '%s\n' "$inputs" "remote-ci-cache-v3"; printf '%s\n' "${REMOTE_CI_NODE:-system}" "${REMOTE_CI_PNPM:-}" "${REMOTE_CI_POSTGRES:-}" "$hash_command"; } |
    shasum -a 256 | awk '{print $1}'
}
