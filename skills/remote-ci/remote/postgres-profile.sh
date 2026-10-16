# ABOUTME: Separate disposable PostgreSQL cluster; caller holds the runner's database guard.
remote_ci_pg_profile_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
pg_profile_boot_id() {
  case "${platform_os:-$(uname -s)}" in
    Darwin) /usr/sbin/sysctl -n kern.bootsessionuuid ;;
    Linux) cat /proc/sys/kernel/random/boot_id ;;
    *) return 1 ;;
  esac
}
pg_profile_configure() {
  if [ -z "${REMOTE_CI_PG_PROFILE:-}" ]; then
    REMOTE_CI_PG_PROFILE=$(host_setting AGENT_CI_PG_PROFILE)
    : "${REMOTE_CI_PG_PROFILE:=durable}"
  fi
  case "$REMOTE_CI_PG_PROFILE" in
    durable|minimal|disposable) export REMOTE_CI_PG_PROFILE ;;
    *) echo 'invalid Postgres durability profile' >&2; return 2 ;;
  esac
  [ -n "${REMOTE_CI_POSTGRES:-}" ] || return 0
  pgdata=$ci/pg/$REMOTE_CI_POSTGRES
  pgport=$((5400 + REMOTE_CI_POSTGRES))
  export PGPORT=$pgport
  case "$REMOTE_CI_PG_PROFILE" in
    durable) ;;
    minimal|disposable)
      pgdata=$ci/pg/$REMOTE_CI_POSTGRES-$REMOTE_CI_PG_PROFILE
      if [ "$REMOTE_CI_PG_PROFILE" = minimal ]; then pgport=$((5600 + REMOTE_CI_POSTGRES))
      else pgport=$((5500 + REMOTE_CI_POSTGRES)); fi
      export PGPORT=$pgport
      ;;
    *) echo 'invalid Postgres durability profile' >&2; return 2 ;;
  esac
}
pg_profile_ensure() {
  local existing_sync="" current_boot="" compact_boot="" rebuild_reason=unclean
  [ "${REMOTE_CI_PG_PROFILE:?Postgres profile not resolved}" != durable ] || return 0
  if [ "$REMOTE_CI_PG_PROFILE" = disposable ]; then
    current_boot=$(pg_profile_boot_id 2>/dev/null) || { echo 'cache postgres boot identity unavailable' >&2; return 74; }
    compact_boot=${current_boot//-/}
    case "$compact_boot" in ''|*[!0-9a-fA-F]*) echo 'cache postgres boot identity unavailable' >&2; return 74 ;; esac
    [ "${#current_boot}" = 36 ] && [ "${#compact_boot}" = 32 ] || return 74
  fi
  if pg_ctl -D "$pgdata" status >/dev/null 2>&1; then
    if [ "$REMOTE_CI_PG_PROFILE" = disposable ] &&
       { [ "$(cat "$pgdata/cache-profile-policy" 2>/dev/null)" != disposable-v4 ] ||
         [ "$(cat "$pgdata/cache-boot-id" 2>/dev/null)" != "$current_boot" ]; }; then
      echo 'cache postgres live policy mismatch; stop the idle CI cluster first' >&2
      return 74
    fi
    return 0
  fi
  if [ -f "$pgdata/PG_VERSION" ]; then
    state=$(LC_ALL=C pg_controldata "$pgdata" | sed -n 's/^Database cluster state: *//p') || return
    if { [ "$REMOTE_CI_PG_PROFILE" = disposable ] &&
         { [ "$state" != 'shut down' ] || [ "$(cat "$pgdata/cache-profile-policy" 2>/dev/null)" != disposable-v4 ] ||
           [ "$(cat "$pgdata/cache-boot-id" 2>/dev/null)" != "$current_boot" ]; }; } ||
       { [ "$state" != 'shut down' ] && [ "$(postgres -D "$pgdata" -C fsync)" = off ]; }; then
      existing_sync=off
      if [ "$REMOTE_CI_PG_PROFILE" = disposable ] && [ "$(cat "$pgdata/cache-boot-id" 2>/dev/null)" != "$current_boot" ]; then
        rebuild_reason=host-boot
      elif [ "$REMOTE_CI_PG_PROFILE" = disposable ] && [ "$(cat "$pgdata/cache-profile-policy" 2>/dev/null)" != disposable-v4 ]; then
        rebuild_reason=policy
      fi
      # Never recover a durability-off cluster. Its old templates die with it.
      bash "$remote_ci_pg_profile_dir/prune.sh" --cache-delete "$pgdata" || return
      echo "cache postgres rebuild reason=$rebuild_reason"
    fi
  fi
  if [ ! -f "$pgdata/PG_VERSION" ]; then
    umask 077
    [ -f "$pgdata.pass" ] || openssl rand -hex 24 >"$pgdata.pass" || return
    python3 "$remote_ci_pg_profile_dir/cache.py" preflight --ci "$ci" || return 75
    initdb -D "$pgdata" -U ci --pwfile="$pgdata.pass" --auth=scram-sha-256 \
      --encoding=UTF8 --locale=C >/dev/null || return
    sync_mode=${existing_sync:-on}
    # Rebuilding an existing off cluster preserves its execution identity.
    if [ -z "$existing_sync" ] && [ -f "$ci/pg/$REMOTE_CI_POSTGRES/PG_VERSION" ]; then
      sync_mode=$(postgres -D "$ci/pg/$REMOTE_CI_POSTGRES" -C fsync) || return
    fi
    checkpoint=30min; wal_size=2GB
    if [ "$REMOTE_CI_PG_PROFILE" = disposable ]; then
      sync_mode=off; checkpoint=1h; wal_size=8GB
    fi
    case "$sync_mode" in on|off) ;; *) return 2 ;; esac
    cat >>"$pgdata/postgresql.conf" <<PROFILE
port = $pgport
listen_addresses = 'localhost'
max_connections = 200
wal_level = minimal
max_wal_senders = 0
archive_mode = off
fsync = $sync_mode
synchronous_commit = $sync_mode
full_page_writes = $sync_mode
checkpoint_timeout = '$checkpoint'
max_wal_size = '$wal_size'
PROFILE
    if declare -F platform_pg_config >/dev/null; then
      platform_pg_config >>"$pgdata/postgresql.conf" || return
    fi
    if [ "$REMOTE_CI_PG_PROFILE" = disposable ]; then
      # New relations need no redundant WAL copy in this rebuild-only cluster.
      # Sparse new WAL files also avoid zero-fill and COW recycling writes.
      printf 'wal_skip_threshold = 0\nwal_init_zero = off\nwal_recycle = off\n' >>"$pgdata/postgresql.conf"
      if [ "$REMOTE_CI_POSTGRES" -ge 18 ]; then
        printf 'file_copy_method = clone\n' >>"$pgdata/postgresql.conf"
      fi
    fi
    printf 'disposable-v4\n' >"$pgdata/cache-profile-policy" || return
    if [ "$REMOTE_CI_PG_PROFILE" = disposable ]; then
      # A stale clean pg_control is unsafe after power loss with fsync off.
      printf '%s\n' "$current_boot" >"$pgdata/cache-boot-id" || return
    fi
    openssl rand -hex 16 >"$pgdata/cache-generation" || return
    # Credentials go only to the private password file, never to a log.
    printf '127.0.0.1:%s:*:ci:%s\n' "$pgport" "$(cat "$pgdata.pass")" >>"$ci/pgpass"
    chmod 600 "$ci/pgpass"
    echo "cache postgres $REMOTE_CI_PG_PROFILE initialized"
  fi
}

# FILE_COPY avoids WAL-logging every template block. PG18 can clone its files.
# Keep durable/minimal projects and pre-PG15 servers on their existing path.
pg_clone_database() {
  local mode=copy reason=wal-log pg_options=${PGOPTIONS:-} strategy=()
  if [ "${REMOTE_CI_PG_PROFILE:?Postgres profile not resolved}" = disposable ] && [ "$REMOTE_CI_POSTGRES" -ge 15 ]; then
    strategy=(--strategy=FILE_COPY)
    reason=postgres-before-18
    if [ "$REMOTE_CI_POSTGRES" -ge 18 ]; then
      mode=clone-requested; reason=none
      pg_options="$pg_options -c file_copy_method=clone"
    fi
  fi
  PGOPTIONS="$pg_options" createdb ${strategy[@]+"${strategy[@]}"} -O "$1" -T "$2" "$3" || return
  echo "cache postgres-copy mode=$mode reason=$reason outcome=complete" >&2
}
