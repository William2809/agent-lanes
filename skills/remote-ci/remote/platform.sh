# ABOUTME: Native tools and paths for macOS and Linux runner scripts.
platform_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
platform_os=$(uname -s)
case "$platform_os" in
  Darwin)
    platform_path=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin
    platform_time=(/usr/bin/time -l)
    platform_locale=en_US.UTF-8
    ;;
  Linux)
    platform_path=$HOME/ci/node/default/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
    platform_locale=C.UTF-8
    if [ -x /usr/bin/time ]; then
      # Explicit fields omit GNU time's command line from the persisted receipt input.
      platform_time=(/usr/bin/time -f $'User time (seconds): %U\nSystem time (seconds): %S\nMaximum resident set size (kbytes): %M\nFile system outputs: %O')
    else platform_time=(python3 "$platform_dir/platform_info.py" time); fi
    ;;
  *) echo 'remote-ci: unsupported runner platform' >&2; return 3 ;;
esac
# Receipt collection always uses the native adapter selected above. Project
# env.local may override platform_time only for the candidate check prefix.
platform_receipt_time=("${platform_time[@]}")
platform_decode() { base64 -d; }
platform_lock() {
  # Keep Darwin interoperable with already-running snapshots during rollout.
  if [ "$platform_os" = Darwin ] && [ -x /usr/bin/shlock ]; then /usr/bin/shlock -f "$1" -p "$2"
  else python3 "$platform_dir/platform_info.py" lock "$1" "$2"; fi
}
platform_pgbin() {
  case "$platform_os" in
    Linux) printf '/usr/lib/postgresql/%s/bin\n' "$1" ;;
    Darwin)
      if [ -x /opt/homebrew/bin/brew ]; then printf '/opt/homebrew/opt/postgresql@%s/bin\n' "$1"
      else printf '/usr/local/opt/postgresql@%s/bin\n' "$1"; fi ;;
  esac
}
platform_pg_config() {
  # PGDG's Linux default socket directory belongs to the system postgres user.
  # The CI account owns its cluster and all clients already use loopback TCP.
  if [ "$platform_os" = Linux ]; then printf "unix_socket_directories = ''\n"; fi
}
