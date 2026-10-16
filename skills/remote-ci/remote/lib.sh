# ABOUTME: Shared helpers for the remote-ci runner scripts on the runner machine (sourced, not executed).
# ABOUTME: Loads a project's config and puts its Node, pnpm and Postgres on PATH.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/platform.sh"
ci=$HOME/ci
. "${BASH_SOURCE[0]%/*}/postgres-profile.sh"
# The public toolkit is produced by literal substitutions. Keep the generated
# toolkit's prefix literal, and assemble both wire-compatible prefixes so either
# client can use either installed kit.
remote_ci_canonical_prefix=REMOTE_CI_
remote_ci_private_prefix=$(printf 'M%s' 'INI_CI_')
remote_ci_public_prefix=$(printf 'R%s' 'EMOTE_CI_')
remote_ci_project_settings='CHECK TIMEOUT WAIT_TIMEOUT SLOTS EXCLUSIVE TEMPLATE_INPUTS POSTGRES DB_PREPARE DB NODE PNPM INSTALL INSTALL_INPUTS LIFECYCLE_INPUTS INSTALL_OUTPUTS SERVICE_IMAGES ENV_REVISION PG_PROFILE DB_CLEAN DATABASE_URL_VAR PREPARE WORKERS MEMORY_MB LEASES NO_DB_COMMANDS NO_DB_PREPARE CACHE_PATHS NEEDS BOOTSTRAP'

# Snapshot only supported runner settings inherited when this library is loaded.
# Values produced by one project load must never become inputs to the next one.
for remote_ci_prefix in "$remote_ci_private_prefix" "$remote_ci_public_prefix"; do
  for remote_ci_suffix in $remote_ci_project_settings; do
    remote_ci_name=$remote_ci_prefix$remote_ci_suffix
    if eval '[ "${'"$remote_ci_name"'+x}" ]'; then
      printf -v "remote_ci_initial_set_$remote_ci_name" '%s' 1
      printf -v "remote_ci_initial_value_$remote_ci_name" '%s' "${!remote_ci_name}"
    fi
  done
done
unset remote_ci_prefix remote_ci_suffix remote_ci_name

remote_ci_reset_project_settings() {
  local prefix suffix name set_name value_name
  for prefix in "$remote_ci_private_prefix" "$remote_ci_public_prefix"; do
    for suffix in $remote_ci_project_settings; do
      name=$prefix$suffix
      set_name=remote_ci_initial_set_$name
      value_name=remote_ci_initial_value_$name
      unset "$name"
      if [ "${!set_name:-0}" = 1 ]; then printf -v "$name" '%s' "${!value_name}"; fi
    done
  done
}
remote_ci_normalize_project_settings() {
  local alternate prefix suffix canonical alternate_name
  if [ "$remote_ci_canonical_prefix" = "$remote_ci_private_prefix" ]; then alternate=$remote_ci_public_prefix
  else alternate=$remote_ci_private_prefix
  fi
  for suffix in $remote_ci_project_settings; do
    canonical=$remote_ci_canonical_prefix$suffix
    alternate_name=$alternate$suffix
    # The canonical spelling wins only when it was explicitly set by the
    # environment or the current project's config.
    if ! eval '[ "${'"$canonical"'+x}" ]' && eval '[ "${'"$alternate_name"'+x}" ]'; then
      printf -v "$canonical" '%s' "${!alternate_name}"
    fi
  done
}
# One host setting from the trusted host config (agent-lanes, then agent-lanes); empty when unset.
host_setting() {
  (for name in agent-lanes agent-lanes; do
    [ -f "$HOME/.config/$name/config" ] && . "$HOME/.config/$name/config" >/dev/null 2>&1
  done; printf '%s' "${!1:-}")
}
load_project() {
  project=$1
  dir=$ci/projects/$project
  local project_conf=${2:-$dir/conf}
  remote_ci_reset_project_settings
  # Resolve settings before choosing any tool identity or cluster path.
  # shellcheck disable=SC1091
  . "$project_conf" || return
  if [ -f "${project_conf%/*}/env.local" ]; then
    . "${project_conf%/*}/env.local" || return
  fi
  remote_ci_normalize_project_settings
  : "${REMOTE_CI_CHECK:=pnpm run check}" "${REMOTE_CI_TIMEOUT:=1500}" "${REMOTE_CI_WAIT_TIMEOUT:=10800}" "${REMOTE_CI_SLOTS:=1}" "${REMOTE_CI_EXCLUSIVE:=0}"
  case "$REMOTE_CI_WAIT_TIMEOUT" in *[!0-9]* | "" | 0*) echo "REMOTE_CI_WAIT_TIMEOUT must be a positive integer" >&2; return 2 ;; esac
  case "$REMOTE_CI_SLOTS" in *[!0-9]* | "" | 0*) echo "REMOTE_CI_SLOTS must be a positive integer" >&2; return 2 ;; esac
  case "$REMOTE_CI_EXCLUSIVE" in 0 | 1) ;; *) echo "REMOTE_CI_EXCLUSIVE must be 0 or 1" >&2; return 2 ;; esac
  # Keep config loading silent: remote() also captures stderr in cache keys.
  exclusive_notice=""
  # A no-exclusive host grants the normal project slot instead.
  if [ "$REMOTE_CI_EXCLUSIVE" = 1 ] && [ "$(host_setting AGENT_CI_ALLOW_EXCLUSIVE)" = 0 ]; then
    REMOTE_CI_EXCLUSIVE=0
    exclusive_notice="remote-ci: exclusive request uses a project slot on this host"
  fi
  # Host-exclusive runs ($ci/lock, drain) are opt-in; every other run takes a project slot.
  if [ "$REMOTE_CI_EXCLUSIVE" = 1 ]; then slotted=0; else slotted=1; fi
  if [ -n "${REMOTE_CI_TEMPLATE_INPUTS:-}" ] && { [ -z "${REMOTE_CI_POSTGRES:-}" ] || [ -z "${REMOTE_CI_DB_PREPARE:-}" ]; }; then
    echo "template inputs require REMOTE_CI_POSTGRES and REMOTE_CI_DB_PREPARE" >&2; return 2
  fi
  : "${REMOTE_CI_DB:=$(printf '%s' "$project" | tr -c 'a-zA-Z0-9\n' '_')_ci}"
  path=$platform_path
  [ -n "${REMOTE_CI_NODE:-}" ] && path=$ci/node/$REMOTE_CI_NODE/bin:$path
  if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
    pgbin=$(platform_pgbin "$REMOTE_CI_POSTGRES")
    path=$pgbin:$path
    export PGHOST=127.0.0.1 PGUSER=ci PGPASSFILE=$ci/pgpass
  fi
  pg_profile_configure || return
  export PATH=$path COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1 LC_ALL=$platform_locale
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
    if ! lock_live "$1" && platform_lock "$1" $$; then return 0; fi
    sleep 1
  done
  return 1
}
acquire_guard() {
  [ "${guard_owned:-0}" = 1 ] && return 0
  acquire_lock "$ci/lock" || return 1
  guard_owned=1
}
release_drain() {
  if [ "${drain_owned:-0}" = 1 ] && [ "$(cat "$ci/drain" 2>/dev/null)" = "$$" ]; then
    # Timestamp first: once the lock is gone a new owner may publish its own drain.at.
    rm -f "$ci/drain.at"
    rm -f "$ci/drain"
  fi
  drain_owned=0
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
    major=$(basename "$data"); major=${major%-disposable}; major=${major%-minimal}
    "$(platform_pgbin "$major")/pg_ctl" -D "$data" stop -m fast >/dev/null 2>&1 || true
  done
}
# Mandatory install inputs and runtime versions also protect database templates.
input_hash() {
  REMOTE_CI_INSTALL="$hash_command" REMOTE_CI_INSTALL_INPUTS="$*" \
    REMOTE_CI_LIFECYCLE_INPUTS="${REMOTE_CI_LIFECYCLE_INPUTS:-}" \
    REMOTE_CI_PG_PROFILE="${REMOTE_CI_PG_PROFILE:?Postgres profile not resolved}" REMOTE_CI_POSTGRES="${REMOTE_CI_POSTGRES:-}" \
    python3 "${BASH_SOURCE[0]%/*}/cache.py" install-key --env-file "${env_file:-/dev/null}"
}
