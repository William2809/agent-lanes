# ABOUTME: Linux package bootstrap. Called only by setup under the host setup lock.
linux_packages() {
  [ "$platform_os" = Linux ] || return 0
  [ -f /etc/os-release ] || { echo 'setup: Linux needs /etc/os-release' >&2; return 3; }
  . /etc/os-release
  case "$ID" in ubuntu|debian) ;; *) echo 'setup: supported Linux packages are Debian/Ubuntu' >&2; return 3 ;; esac
  # Admin rights are explicit and noninteractive; never prompt during a queued run.
  local admin=() package missing=()
  [ "$(id -u)" = 0 ] || admin=(sudo -n)
  for package in ca-certificates curl git python3 python3-venv perl openssl time util-linux xz-utils libatomic1; do
    [ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null)" = 'install ok installed' ] || missing+=("$package")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    "${admin[@]}" apt-get update -qq
    "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
  if [ -n "${REMOTE_CI_POSTGRES:-}" ] && [ ! -x "$pgbin/postgres" ]; then
    "${admin[@]}" install -d /usr/share/postgresql-common/pgdg
    "${admin[@]}" curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc.new \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc
    "${admin[@]}" mv /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc.new /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
    printf 'Types: deb\nURIs: https://apt.postgresql.org/pub/repos/apt\nSuites: %s-pgdg\nComponents: main\nSigned-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n' "$VERSION_CODENAME" |
      "${admin[@]}" tee /etc/apt/sources.list.d/remote-ci-pgdg.sources.new >/dev/null
    "${admin[@]}" mv /etc/apt/sources.list.d/remote-ci-pgdg.sources.new /etc/apt/sources.list.d/remote-ci-pgdg.sources
    "${admin[@]}" apt-get update -qq
    # Disable creation of an unrelated system cluster. Our cluster is user-owned.
    "${admin[@]}" apt-get install -y -qq postgresql-common
    "${admin[@]}" install -d /etc/postgresql-common/createcluster.d
    printf 'create_main_cluster = false\n' | "${admin[@]}" tee /etc/postgresql-common/createcluster.d/remote-ci.conf >/dev/null
    "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-$REMOTE_CI_POSTGRES"
  fi
  case " ${REMOTE_CI_NEEDS:-} " in
    *' docker '*) docker info >/dev/null 2>&1 || { echo 'setup: Docker daemon unavailable' >&2; return 3; } ;;
    *) if command -v docker >/dev/null 2>&1; then
         docker info >/dev/null 2>&1 || { echo 'setup: Docker daemon unavailable' >&2; return 3; }
       else echo 'setup: Docker not installed (no docker capability requested)'; fi ;;
  esac
}
