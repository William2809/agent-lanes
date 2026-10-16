#!/bin/bash
# ABOUTME: Runner side of `remote-ci install-runner`: counts live runs, or makes a staged kit the host kit (~/ci/kit).
# ABOUTME: Install also refreshes ~/ci/bin (prune, status) and every project repo's protocol hook.
# Usage: install-kit.sh count
#        install-kit.sh install <staged dir> <kit rev> <protocol> <source rev>
set -euo pipefail
ci=$HOME/ci
case "${1:-}" in
count)
  # Scheduler owners: runs (and --cleanup children), first-time setup and prune.
  procs=$(ps -axo command=)
  shell_count=$(printf '%s\n' "$procs" | awk -v h="$ci/" '
    ($1 == "bash" || $1 == "/bin/bash") && index($2, h) == 1 &&
      $2 ~ /\/(run|setup|prune)\.sh$/ { n++ }
    index($2, h) == 1 && ($2 ~ /\/scheduler_(maintenance|process)\.py$/ ||
      ($2 ~ /\/resources\.py$/ && $3 == "acquire")) { n++ }
    END { print n + 0 }')
  if [ -f "$ci/freeze" ]; then
    python3 "$(cd "$(dirname "$0")" && pwd)/resources.py" recover-pending
  fi
  # Include recorded orphan groups. A bare Python phase is not a bash runner.
  lease_count=$(python3 "$(cd "$(dirname "$0")" && pwd)/resources.py" active-count)
  echo "$((shell_count + lease_count))"
  ;;
install)
  stage=$2 rev=$3 protocol=$4 source=$5
  [ "$(bash "$0" count)" = 0 ] || { echo "install-kit: unresolved leases; keep runner frozen" >&2; exit 75; }
  case "$rev$protocol$source" in *[!0-9a-f]*) echo "install-kit: bad arguments" >&2; exit 2 ;; esac
  echo "$rev" >"$stage/VERSION"
  echo "$protocol" >"$stage/PROTOCOL"
  echo "$source" >"$stage/SOURCE"
  chmod 755 "$stage"
  target=$ci/kits/$rev-$(date +%Y%m%d%H%M%S)
  mv "$stage" "$target"
  # Repos first: advertise push options and install the floor before any client sees the new kit.
  for repo in "$ci"/projects/*/repo.git; do
    [ -d "$repo" ] || continue
    git -C "$repo" config receive.advertisePushOptions true
    cp "$target/pre-receive.sh" "$repo/hooks/pre-receive.new"
    chmod 755 "$repo/hooks/pre-receive.new"
    mv -f "$repo/hooks/pre-receive.new" "$repo/hooks/pre-receive"
  done
  # ~/ci/bin keeps matching copies for prune (launchd) and older status calls; prune loads its own
  # directory's lib.sh, so an old client's upload there stays a consistent pair.
  mkdir -p "$ci/bin"
  for script in "$target"/*.sh "$target"/*.py; do
    cp -p "$script" "$ci/bin/.$(basename "$script").new"
    mv -f "$ci/bin/.$(basename "$script").new" "$ci/bin/$(basename "$script")"
  done
  # Atomic switch: rename(2) replaces the ~/ci/kit symlink itself, never a directory it points to.
  ln -sfn "kits/$(basename "$target")" "$ci/kit.new"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$ci/kit.new" "$ci/kit"
  echo "remote-ci: host kit $rev (protocol $protocol, source $source) installed"
  ;;
*) echo "usage: install-kit.sh count | install <stage> <rev> <protocol> <source>" >&2; exit 2 ;;
esac
